#!/usr/bin/env python3
"""Incrementally mirror one public upstream operator-voice directory."""

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


def destination(source_path: str, base_path: str) -> str | None:
    prefix = base_path.rstrip("/") + "/"
    if not source_path.startswith(prefix):
        return None
    relative = PurePosixPath(source_path[len(prefix):])
    if len(relative.parts) < 2 or relative.suffix.lower() != ".mp3":
        return None
    return str(PurePosixPath("current") / relative)


def raw_url(repository: str, commit: str, path: str) -> str:
    owner_repo = repository.removeprefix("https://github.com/").removesuffix(".git")
    return f"https://raw.githubusercontent.com/{owner_repo}/{commit}/{urllib.parse.quote(path, safe='/')}"


def download(url: str) -> bytes:
    return subprocess.check_output([
        "curl", "--fail", "--silent", "--show-error", "--location",
        "--retry", "3", "--max-time", "120", "--user-agent", "arkpedia-voice-sync/1", url,
    ])


def stage(paths: list[str]) -> None:
    for offset in range(0, len(paths), 100):
        run("git", "add", "--sparse", "--", *paths[offset:offset + 100], cwd=ROOT)


def main() -> None:
    source = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    old_commit = source["sourceCommit"]
    upstream, new_commit = ensure_upstream(
        source["sourceRepository"], source["sourceBranch"], old_commit, ROOT / ".cache/upstream-voices"
    )
    if new_commit == old_commit:
        print(f"Already current at {new_commit[:12]}.")
        return

    changes: list[tuple[str, str, str]] = []
    for status, path in changes_under(upstream, old_commit, new_commit, source["path"]):
        target = destination(path, source["path"])
        if target:
            changes.append((status, path, target))

    removed = [path for status, path, _ in changes if status == 'D']
    if removed:
        raise RuntimeError(f'Upstream removed {len(removed)} recordings; review archival/mapping before syncing. Nothing will be deleted.')
    if len(changes) > 1500:
        raise RuntimeError(f'Unexpected bulk voice replacement ({len(changes)}); review upstream before proceeding.')

    downloaded: dict[str, bytes] = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {
            pool.submit(download, raw_url(source["sourceRepository"], new_commit, path)): path
            for status, path, _ in changes if status != "D"
        }
        for future in as_completed(futures):
            path = futures[future]
            data = future.result()
            if not 0 < len(data) < 100 * 1024 * 1024:
                raise ValueError(f'Empty or oversized audio: {path}')
            expected = run('git', 'rev-parse', f'{new_commit}:{path}', cwd=upstream, capture=True)
            actual = hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()
            if actual != expected:
                raise ValueError(f'Upstream voice hash mismatch: {path}')
            # Decode every changed recording before any files are staged.
            subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', 'pipe:0', '-f', 'null', '-'],
                           input=data, check=True, timeout=60)
            downloaded[path] = data

    staged: list[str] = []
    added_or_updated = deleted = 0
    for status, upstream_path, target_path in changes:
        target = ROOT / target_path
        if status == "D":
            run("git", "update-index", "--force-remove", "--", target_path, cwd=ROOT)
            deleted += 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(downloaded[upstream_path])
            staged.append(target_path)
            added_or_updated += 1

    source["sourceCommit"] = new_commit
    source["importedOn"] = datetime.now(timezone.utc).date().isoformat()
    SOURCE_FILE.write_text(json.dumps(source, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    stage(staged + [SOURCE_FILE.name])
    print(
        f"Advanced source to {new_commit[:12]}; synced {added_or_updated} added/updated "
        f"and {deleted} deleted clips."
    )


if __name__ == "__main__":
    main()
