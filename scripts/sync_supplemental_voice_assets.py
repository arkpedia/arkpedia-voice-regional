#!/usr/bin/env python3
"""Incrementally mirror MP3 recordings from a supplemental public voice source."""

from __future__ import annotations

import hashlib
import json
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILE = ROOT / "source.json"


def run(*args: str, cwd: Path | None = None, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def ensure_upstream(url: str, branch: str, old_commit: str, cache: Path) -> tuple[Path, str]:
    if not (cache / ".git").is_dir():
        cache.parent.mkdir(parents=True, exist_ok=True)
        run(
            "git", "clone", "--filter=blob:none", "--no-checkout", "--single-branch",
            "--branch", branch, "--depth=1", url, str(cache),
        )
    else:
        run("git", "remote", "set-url", "origin", url, cwd=cache)
    run("git", "fetch", "--filter=blob:none", "--depth=1", "origin", branch, cwd=cache)
    new_commit = run("git", "rev-parse", "FETCH_HEAD", cwd=cache, capture=True)
    if old_commit and subprocess.run(
        ["git", "cat-file", "-e", f"{old_commit}^{{commit}}"], cwd=cache
    ).returncode:
        run("git", "fetch", "--filter=blob:none", "--depth=1", "origin", old_commit, cwd=cache)
    return cache, new_commit


def changes_under(repo: Path, old_commit: str, new_commit: str, base_path: str) -> list[tuple[str, str]]:
    raw = subprocess.check_output(
        ["git", "diff", "--name-status", "--no-renames", "-z", old_commit, new_commit, "--", base_path],
        cwd=repo,
    )
    fields = raw.decode("utf-8").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        raise RuntimeError("Unexpected git diff output")
    return [(fields[index], fields[index + 1]) for index in range(0, len(fields), 2)]


def source_assets(repo: Path, commit: str, base_path: str) -> list[str]:
    raw = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "-z", commit, "--", base_path],
        cwd=repo,
    )
    return [path for path in raw.decode("utf-8").split("\0") if path]


def tracked_assets() -> set[str]:
    raw = subprocess.check_output(["git", "ls-files", "-z", "--", "current"], cwd=ROOT)
    return {path for path in raw.decode("utf-8").split("\0") if path}


def destination(source_path: str, base_path: str) -> str | None:
    prefix = base_path.rstrip("/") + "/"
    if not source_path.startswith(prefix):
        return None
    relative = PurePosixPath(source_path[len(prefix):])
    if len(relative.parts) < 2 or relative.suffix.lower() != ".mp3":
        return None
    return str(PurePosixPath("current", *relative.parts[:-1], relative.name.lower()))


def raw_url(repository: str, commit: str, asset_path: str) -> str:
    owner_repo = repository.removeprefix("https://github.com/").removesuffix(".git")
    encoded_path = urllib.parse.quote(asset_path, safe="/")
    return f"https://raw.githubusercontent.com/{owner_repo}/{commit}/{encoded_path}"


def download(url: str) -> bytes:
    return subprocess.check_output([
        "curl", "--fail", "--silent", "--show-error", "--location",
        "--retry", "3", "--max-time", "120", "--user-agent", "arkpedia-voice-sync/1", url,
    ])


def stage(paths: list[str]) -> None:
    for offset in range(0, len(paths), 100):
        run("git", "add", "--sparse", "--", *paths[offset:offset + 100], cwd=ROOT)


def main() -> None:
    manifest = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    source = manifest.get("supplementalSource")
    if not source:
        print("No supplemental voice source configured.")
        return

    old_commit = source["sourceCommit"]
    upstream, new_commit = ensure_upstream(
        source["sourceRepository"],
        source["sourceBranch"],
        old_commit,
        ROOT / ".cache/upstream-voices-supplemental",
    )
    commit_changed = new_commit != old_commit
    changes_by_target: dict[str, tuple[str, str, str]] = {}
    if commit_changed:
        for status, asset_path in changes_under(upstream, old_commit, new_commit, source["path"]):
            target = destination(asset_path, source["path"])
            if target:
                changes_by_target[target] = (status, asset_path, target)

    # The supplemental mirror is also an inventory fallback. Checking the full
    # tree repairs clips that predate the saved source commit but are absent from
    # this repository, instead of waiting for those paths to change upstream.
    tracked = tracked_assets()
    for asset_path in source_assets(upstream, new_commit, source["path"]):
        target = destination(asset_path, source["path"])
        if target and target not in tracked and target not in changes_by_target:
            changes_by_target[target] = ("A", asset_path, target)

    changes = list(changes_by_target.values())

    removed = [asset_path for status, asset_path, _ in changes if status == "D"]
    if removed:
        raise RuntimeError(
            f"Supplemental source removed {len(removed)} recordings; review before syncing."
        )
    if len(changes) > 1500:
        raise RuntimeError(
            f"Unexpected bulk supplemental replacement ({len(changes)}); review upstream."
        )

    downloaded: dict[str, bytes] = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {
            pool.submit(download, raw_url(source["sourceRepository"], new_commit, asset_path)): asset_path
            for status, asset_path, _ in changes
        }
        for future in as_completed(futures):
            asset_path = futures[future]
            data = future.result()
            if not 0 < len(data) < 100 * 1024 * 1024:
                raise ValueError(f"Empty or oversized audio: {asset_path}")
            expected = run("git", "rev-parse", f"{new_commit}:{asset_path}", cwd=upstream, capture=True)
            actual = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            if actual != expected:
                raise ValueError(f"Upstream voice hash mismatch: {asset_path}")
            subprocess.run(
                ["ffmpeg", "-v", "error", "-xerror", "-i", "pipe:0", "-f", "null", "-"],
                input=data,
                check=True,
                timeout=60,
            )
            downloaded[asset_path] = data

    staged: list[str] = []
    for _, asset_path, target_path in changes:
        target = ROOT / target_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(downloaded[asset_path])
        staged.append(target_path)

    metadata: list[str] = []
    if commit_changed:
        source["sourceCommit"] = new_commit
        source["importedOn"] = datetime.now(timezone.utc).date().isoformat()
        SOURCE_FILE.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        metadata.append(SOURCE_FILE.name)
    stage(staged + metadata)
    if staged:
        print(f"Checked supplemental source {new_commit[:12]}; synced {len(staged)} clips.")
    else:
        print(f"Supplemental source and inventory are current at {new_commit[:12]}.")


if __name__ == "__main__":
    main()
