"""Offline Git fixtures for primary ownership and supplemental sync safeguards."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import sync_supplemental_voice_assets as sync


RUN = subprocess.run


def git(repo: Path, *args: str) -> str:
    return RUN(
        ["git", *args], cwd=repo, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    git(repo, "init", "--quiet")
    git(repo, "config", "user.name", "Voice sync fixture")
    git(repo, "config", "user.email", "voice-sync@example.invalid")


def commit_files(repo: Path, files: dict[str, bytes | None]) -> str:
    for name, data in files.items():
        path = repo / name
        if data is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "fixture")
    return git(repo, "rev-parse", "HEAD")


class SupplementalSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = base / "destination"
        self.primary = base / "primary"
        self.supplemental = base / "supplemental"
        for repo in (self.root, self.primary, self.supplemental):
            init_repo(repo)
        primary_pin = commit_files(self.primary, {"primary/actor/primary.mp3": b"primary-old"})
        self.supplemental_pin = commit_files(self.supplemental, {
            "voices/actor/primary.mp3": b"supplemental-primary-old",
            "voices/actor/extra.mp3": b"extra-old",
        })
        self.manifest = {
            "sourceRepository": str(self.primary),
            "sourceBranch": "main",
            "sourceCommit": primary_pin,
            "path": "primary",
            "supplementalSource": {
                "sourceRepository": "https://github.com/fixture/supplemental",
                "sourceBranch": "main",
                "sourceCommit": self.supplemental_pin,
                "path": "voices",
            },
        }
        commit_files(self.root, {
            ".gitignore": b".cache/\n",
            "source.json": json.dumps(self.manifest).encode(),
            "current/actor/primary.mp3": b"primary-old",
            "current/actor/extra.mp3": b"extra-old",
        })
        self.cache = self.root / ".cache/upstream-voices"
        git(self.root, "clone", "--quiet", "--no-checkout", str(self.primary), str(self.cache))
        self.downloaded: list[str] = []
        self.decoded: list[bytes] = []

    def save_manifest(self) -> None:
        (self.root / "source.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def update_supplemental(self, files: dict[str, bytes | None]) -> None:
        self.supplemental_pin = commit_files(self.supplemental, files)

    def update_primary(self, files: dict[str, bytes], *, advance_pin: bool = True) -> None:
        pin = commit_files(self.primary, files)
        git(self.cache, "fetch", "--quiet", "origin")
        if advance_pin:
            self.manifest["sourceCommit"] = pin
            self.save_manifest()
            for path, data in files.items():
                target = sync.primary_destination(path, self.manifest["path"])
                if target:
                    (self.root / target).write_bytes(data)
                    git(self.root, "add", "--", target)

    def download(self, url: str) -> bytes:
        _, _, commit, path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lstrip("/").split("/", 3)
        self.downloaded.append(path)
        return subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=self.supplemental)

    def run_without_decoder(self, args, **kwargs):
        # These fixtures exercise Git blob hashes and staging, not MP3 encoding.
        if args[0] == "ffmpeg":
            self.decoded.append(kwargs["input"])
            return subprocess.CompletedProcess(args, 0)
        return RUN(args, **kwargs)

    def sync(self, *, download=None) -> None:
        with (
            patch.object(sync, "ROOT", self.root),
            patch.object(sync, "SOURCE_FILE", self.root / "source.json"),
            patch.object(sync, "ensure_upstream", return_value=(self.supplemental, self.supplemental_pin)),
            patch.object(sync, "download", side_effect=download or self.download),
            patch.object(sync.subprocess, "run", side_effect=self.run_without_decoder),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            sync.main()

    def assert_audio(self, name: str, expected: bytes) -> None:
        self.assertEqual((self.root / "current/actor" / name).read_bytes(), expected)

    def test_primary_owned_update_is_skipped_and_metadata_advances(self) -> None:
        self.update_supplemental({"voices/actor/primary.mp3": b"supplemental-primary-new"})
        self.sync()
        self.assert_audio("primary.mp3", b"primary-old")
        self.assertEqual(self.downloaded, [])
        saved = json.loads((self.root / "source.json").read_text())
        self.assertEqual(saved["supplementalSource"]["sourceCommit"], self.supplemental_pin)
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "source.json")

    def test_both_sources_update_primary_stays_authoritative(self) -> None:
        self.update_primary({"primary/actor/primary.mp3": b"primary-new"})
        self.update_supplemental({"voices/actor/primary.mp3": b"supplemental-primary-new"})
        self.sync()
        self.assert_audio("primary.mp3", b"primary-new")
        self.assertEqual(self.downloaded, [])

    def test_new_primary_recording_takes_ownership(self) -> None:
        self.update_primary({"primary/actor/extra.mp3": b"primary-extra"})
        self.update_supplemental({"voices/actor/extra.mp3": b"extra-new"})
        self.sync()
        self.assert_audio("extra.mp3", b"primary-extra")

    def test_supplemental_owned_update_and_new_recording_are_synced(self) -> None:
        self.update_supplemental({
            "voices/actor/extra.mp3": b"extra-new",
            "voices/actor/NEW.MP3": b"new-clip",
        })
        self.sync()
        self.assert_audio("extra.mp3", b"extra-new")
        self.assert_audio("new.mp3", b"new-clip")
        self.assertCountEqual(self.decoded, [b"extra-new", b"new-clip"])
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only").splitlines(), [
            "current/actor/extra.mp3", "current/actor/new.mp3", "source.json",
        ])

    def test_gap_fill_uses_primary_tree_when_local_files_are_missing(self) -> None:
        git(self.root, "rm", "--quiet", "current/actor/primary.mp3", "current/actor/extra.mp3")
        self.sync()
        self.assertFalse((self.root / "current/actor/primary.mp3").exists())
        self.assert_audio("extra.mp3", b"extra-old")
        self.assertEqual(self.downloaded, ["voices/actor/extra.mp3"])

    def test_ownership_uses_pinned_commit_not_later_primary_tree(self) -> None:
        self.update_primary({"primary/actor/extra.mp3": b"later-primary"}, advance_pin=False)
        self.update_supplemental({"voices/actor/extra.mp3": b"extra-new"})
        self.sync()
        self.assert_audio("extra.mp3", b"extra-new")

    def test_primary_mapping_does_not_claim_wav_or_differently_cased_target(self) -> None:
        self.update_primary({
            "primary/actor/extra.wav": b"primary-wav",
            "primary/actor/NEW.MP3": b"primary-uppercase",
        })
        self.update_supplemental({
            "voices/actor/extra.mp3": b"extra-new",
            "voices/actor/NEW.MP3": b"supplemental-lowercase",
        })
        self.sync()
        self.assert_audio("extra.mp3", b"extra-new")
        self.assert_audio("NEW.MP3", b"primary-uppercase")
        self.assert_audio("new.mp3", b"supplemental-lowercase")

    def test_cold_cache_fetches_exact_pin(self) -> None:
        self.update_primary({"primary/actor/extra.mp3": b"later-primary"}, advance_pin=False)
        shutil.rmtree(self.cache)
        self.update_supplemental({"voices/actor/extra.mp3": b"extra-new"})
        self.sync()
        self.assert_audio("extra.mp3", b"extra-new")
        self.assertEqual(git(self.cache, "rev-parse", "FETCH_HEAD"), self.manifest["sourceCommit"])
        self.assertEqual(git(self.cache, "remote", "get-url", "origin"), str(self.primary))

    def test_cached_pin_needs_no_primary_fetch(self) -> None:
        self.update_supplemental({"voices/actor/extra.mp3": b"extra-new"})
        self.manifest["sourceRepository"] = str(self.root / "missing-primary-repository")
        self.save_manifest()
        self.sync()
        self.assert_audio("extra.mp3", b"extra-new")

    def test_unavailable_primary_pin_fails_before_writing(self) -> None:
        self.manifest["sourceCommit"] = "0" * 40
        self.save_manifest()
        before = (self.root / "source.json").read_bytes()
        self.update_supplemental({"voices/actor/extra.mp3": b"extra-new"})
        with self.assertRaises(subprocess.CalledProcessError):
            self.sync()
        self.assert_audio("extra.mp3", b"extra-old")
        self.assertEqual((self.root / "source.json").read_bytes(), before)
        self.assertEqual(self.downloaded, [])

    def test_supplemental_deletion_does_not_block_primary_owned_recording(self) -> None:
        self.update_supplemental({"voices/actor/primary.mp3": None})
        self.sync()
        self.assert_audio("primary.mp3", b"primary-old")
        saved = json.loads((self.root / "source.json").read_text())
        self.assertEqual(saved["supplementalSource"]["sourceCommit"], self.supplemental_pin)
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "source.json")

    def test_supplemental_owned_deletion_still_requires_review(self) -> None:
        self.update_supplemental({"voices/actor/extra.mp3": None})
        with self.assertRaisesRegex(RuntimeError, "removed 1 recordings"):
            self.sync()
        self.assert_audio("extra.mp3", b"extra-old")
        self.assertEqual(self.downloaded, [])

    def test_supplemental_owned_bulk_changes_still_require_review(self) -> None:
        self.update_supplemental({f"voices/actor/clip-{n}.mp3": b"clip" for n in range(1501)})
        with self.assertRaisesRegex(RuntimeError, "bulk supplemental replacement"):
            self.sync()
        self.assertEqual(self.downloaded, [])
        self.assertFalse((self.root / "current/actor/clip-0.mp3").exists())

    def test_bad_download_hash_fails_before_writing(self) -> None:
        self.update_supplemental({"voices/actor/extra.mp3": b"extra-new"})
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.sync(download=lambda _: b"corrupt")
        self.assert_audio("extra.mp3", b"extra-old")
        self.assertEqual(self.decoded, [])
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")

    def test_no_supplemental_source_does_not_resolve_primary(self) -> None:
        self.manifest.pop("supplementalSource")
        self.save_manifest()
        with patch.object(sync, "primary_targets", side_effect=AssertionError("unexpected lookup")):
            self.sync()


if __name__ == "__main__":
    unittest.main()
