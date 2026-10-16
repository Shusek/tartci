#!/usr/bin/env python3
"""Export a private fleet source at one commit, read through the GitHub API.

Self-update keeps one immutable directory per commit under the store
(``<store>/<commit>/``). Each holds the source's ``profiles/*-macos-fleet.toml``
and ``fleet/advertised-labels.json`` plus the marker that fleet_source checks.
An agent installed at one commit keeps reading that commit's directory while a
later commit is exported beside it, so a pinned environment never goes stale
underneath a running agent.

Reads go through the GitHub API (the same ``ghapp api`` path self-update uses
for the TartCI repository), so no git credential is needed. Each file's bytes
are checked against the blob id the tree reported before anything is written.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import Callable

import fleet_source


# OWNER/REPO; each part starts with a letter or digit, so "." and ".." never pass.
REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
WANTED = re.compile(r"^(?:profiles/[^/]+-macos-fleet\.toml|fleet/advertised-labels\.json)$")
STAGING_PREFIX = ".staging."
KEEP = 3
STAGING_MAX_AGE = 3600

# One GitHub API read: the path after `api`, returning (rc, stdout, stderr).
Api = Callable[[str], "tuple[int, str, str]"]


class SyncError(RuntimeError):
    pass


def _json(api: Api, path: str) -> dict:
    rc, out, err = api(path)
    if rc != 0:
        raise SyncError(f"{path}: {(err or out).strip()[:200] or f'exit {rc}'}")
    try:
        value = json.loads(out)
    except json.JSONDecodeError as exc:
        raise SyncError(f"{path}: not JSON") from exc
    if not isinstance(value, dict):
        raise SyncError(f"{path}: not a JSON object")
    return value


def head(api: Api, repository: str, ref: str = "main") -> tuple[str, str]:
    """The commit and tree ids at ref."""
    if not REPOSITORY.fullmatch(repository):
        raise SyncError(f"not an OWNER/REPO repository: {repository!r}")
    value = _json(api, f"repos/{repository}/commits/{ref}")
    commit = value.get("sha")
    details = value.get("commit")
    tree_ref = details.get("tree") if isinstance(details, dict) else None
    tree = tree_ref.get("sha") if isinstance(tree_ref, dict) else None
    if not isinstance(commit, str) or not fleet_source.COMMIT.fullmatch(commit):
        raise SyncError(f"repos/{repository}/commits/{ref}: no commit id")
    if not isinstance(tree, str) or not fleet_source.COMMIT.fullmatch(tree):
        raise SyncError(f"repos/{repository}/commits/{ref}: no tree id")
    return commit, tree


def _make_writable(root: Path) -> None:
    for dirpath, _dirs, files in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in files:
            os.chmod(os.path.join(dirpath, name), 0o644)


def _remove(path: Path) -> None:
    if path.exists():
        _make_writable(path)
        shutil.rmtree(path)


def _freeze(root: Path) -> None:
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            os.chmod(os.path.join(dirpath, name), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.chmod(dirpath, 0o555)


def export(api: Api, repository: str, commit: str, tree: str, store: Path) -> Path:
    """Return store/<commit>, exporting it first unless an intact copy exists."""
    if not REPOSITORY.fullmatch(repository):
        raise SyncError(f"not an OWNER/REPO repository: {repository!r}")
    if not fleet_source.COMMIT.fullmatch(commit):
        raise SyncError(f"not a commit id: {commit!r}")
    dest = store / commit
    if dest.is_dir() and fleet_source.marker_problem(dest, repository, commit) is None:
        return dest
    listing = _json(api, f"repos/{repository}/git/trees/{tree}?recursive=1")
    if listing.get("truncated"):
        raise SyncError(f"{repository}@{commit[:12]}: tree listing is truncated")
    entries = [entry for entry in listing.get("tree") or []
               if isinstance(entry, dict) and entry.get("type") == "blob"
               and isinstance(entry.get("path"), str) and WANTED.fullmatch(entry["path"])]
    paths = {entry["path"] for entry in entries}
    if fleet_source.PUBLISHED_SUPPLY not in paths:
        raise SyncError(f"{repository}@{commit[:12]} has no {fleet_source.PUBLISHED_SUPPLY}")
    if not any(path.startswith("profiles/") for path in paths):
        raise SyncError(f"{repository}@{commit[:12]} has no profiles/*-macos-fleet.toml")
    store.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=store))
    try:
        for entry in entries:
            if not isinstance(entry.get("sha"), str) or not fleet_source.COMMIT.fullmatch(entry["sha"]):
                raise SyncError(f"{entry['path']}: tree entry has no blob id")
            blob = _json(api, f"repos/{repository}/git/blobs/{entry['sha']}")
            if blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
                raise SyncError(f"{entry['path']}: blob is not base64 content")
            data = base64.b64decode(blob["content"])
            if fleet_source.blob_sha(data) != entry.get("sha"):
                raise SyncError(f"{entry['path']}: content does not match blob {entry.get('sha')}")
            target = staging / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        fleet_source.write_marker(staging, repository, commit)
        problem = fleet_source.marker_problem(staging, repository, commit)
        if problem:
            raise SyncError(f"export of {repository}@{commit[:12]} is not intact: {problem}")
        _freeze(staging)
        _remove(dest)  # a damaged earlier copy
        os.rename(staging, dest)
    except BaseException:
        _remove(staging)
        raise
    return dest


def prune(store: Path, keep_commits: set[str], keep: int = KEEP,
          now: float | None = None) -> list[str]:
    """Remove exports beyond the newest `keep` that nothing names; return them."""
    if not store.is_dir():
        return []
    now = time.time() if now is None else now
    removed = []
    exports = []
    for path in store.iterdir():
        if path.name.startswith(STAGING_PREFIX):
            if now - path.stat().st_mtime > STAGING_MAX_AGE:
                _remove(path)
                removed.append(path.name)
        elif path.is_dir() and fleet_source.COMMIT.fullmatch(path.name):
            exports.append(path)
    exports.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for path in exports[keep:]:
        if path.name not in keep_commits:
            _remove(path)
            removed.append(path.name)
    return removed
