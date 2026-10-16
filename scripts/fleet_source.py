#!/usr/bin/env python3
"""Resolve the fleet source used by TartCI's offline tooling.

The public checkout remains the default. Operators may point a process at an
installed private source with ``TARTCI_FLEET_SOURCE_ROOT``. An explicit source
is validated and fails closed; it never silently falls back to the checkout.

An explicit source carries a marker (MARKER) written when it was exported at
one commit. The marker names the repository and commit and the git blob hash
of every file, so the pinned commit is proved by the files themselves.
"""

from __future__ import annotations

import os
import re
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


PROFILE_GLOB = "*-macos-fleet.toml"
MARKER = ".tartci-fleet-source.json"
MARKER_SCHEMA = "tartci.fleet-source/v1"
PUBLISHED_SUPPLY = "fleet/advertised-labels.json"
COMMIT = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class FleetSource:
    root: Path
    profiles_dir: Path
    published_supply: Path
    configured: bool

    @property
    def source_commit(self) -> str | None:
        if not self.configured:
            return None
        value = os.environ.get("TARTCI_FLEET_SOURCE_COMMIT", "").strip()
        return value or None

    @property
    def repository(self) -> str | None:
        if not self.configured:
            return None
        value = os.environ.get("TARTCI_FLEET_SOURCE_REPOSITORY", "").strip()
        return value or None

    @property
    def published_supply_sha256(self) -> str:
        return hashlib.sha256(self.published_supply.read_bytes()).hexdigest()

    @property
    def tree_sha256(self) -> str:
        digest = hashlib.sha256()
        paths = [*sorted(self.profiles_dir.glob(PROFILE_GLOB)), self.published_supply]
        for path in paths:
            _update_digest(digest, path, self.root)
        return digest.hexdigest()

    @property
    def profiles_sha256(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.profiles_dir.glob(PROFILE_GLOB)):
            _update_digest(digest, path, self.root)
        return digest.hexdigest()


def _update_digest(digest: hashlib._Hash, path: Path, root: Path) -> None:
    """Hash path names and bytes in a stable, length-delimited form."""
    relative = path.relative_to(root).as_posix().encode()
    digest.update(len(relative).to_bytes(8, "big"))
    digest.update(relative)
    content = path.read_bytes()
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)


def blob_sha(data: bytes) -> str:
    """The git blob id of data, as the GitHub tree API reports it."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def source_files(root: Path) -> list[str]:
    """The relative paths an exported source carries, sorted."""
    paths = [path.relative_to(root).as_posix()
             for path in (root / "profiles").glob(PROFILE_GLOB) if path.is_file()]
    if (root / PUBLISHED_SUPPLY).is_file():
        paths.append(PUBLISHED_SUPPLY)
    return sorted(paths)


def write_marker(root: Path, repository: str, commit: str) -> Path:
    """Record repository, commit and the blob hash of every source file."""
    files = {path: blob_sha((root / path).read_bytes()) for path in source_files(root)}
    marker = root / MARKER
    marker.write_text(json.dumps({"schema": MARKER_SCHEMA, "repository": repository,
                                  "commit": commit, "files": files},
                                 indent=2, sort_keys=True) + "\n")
    return marker


def marker_problem(root: Path, repository: str, commit: str) -> str | None:
    """Why root is not an intact export of repository at commit, or None."""
    try:
        marker = json.loads((root / MARKER).read_text())
    except (OSError, json.JSONDecodeError):
        return f"no readable {MARKER}"
    if not isinstance(marker, dict) or marker.get("schema") != MARKER_SCHEMA:
        return f"{MARKER} is not a {MARKER_SCHEMA} document"
    if marker.get("repository") != repository or marker.get("commit") != commit:
        return (f"exported from {marker.get('repository')}@{marker.get('commit')}, "
                f"not {repository}@{commit}")
    files = marker.get("files")
    if not isinstance(files, dict) or sorted(files) != source_files(root):
        return "its files differ from the files recorded at export"
    for path, expected in files.items():
        if blob_sha((root / path).read_bytes()) != expected:
            return f"{path} changed after export"
    return None


def resolve(default_root: Path, *, allow_environment: bool = True) -> FleetSource:
    """Return the active source, rejecting an invalid explicit source."""
    configured_root = (
        os.environ.get("TARTCI_FLEET_SOURCE_ROOT", "").strip()
        if allow_environment else ""
    )
    root = Path(configured_root).expanduser() if configured_root else default_root
    root = root.resolve()
    profiles_dir = root / "profiles"
    published_supply = root / "fleet" / "advertised-labels.json"
    if configured_root:
        if not profiles_dir.is_dir():
            raise ValueError(f"configured fleet source has no profiles directory: {profiles_dir}")
        if not published_supply.is_file():
            raise ValueError(
                "configured fleet source has no published supply: "
                f"{published_supply}"
            )
        if not list(profiles_dir.glob(PROFILE_GLOB)):
            raise ValueError(f"configured fleet source has no macOS fleet profiles: {profiles_dir}")
        try:
            published = json.loads(published_supply.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"configured fleet source has invalid published supply: {published_supply}"
            ) from exc
        if (not isinstance(published, dict)
                or published.get("schema") != "tartci.advertised-labels/v1"
                or not isinstance(published.get("registrations"), list)
                or not isinstance(published.get("generated_from"), dict)):
            raise ValueError(
                "configured fleet source published supply must be a "
                "tartci.advertised-labels/v1 document"
            )
        generated = published["generated_from"]
        # generated_from.repo and .commit are informational: a file cannot name
        # the commit that contains it. The marker proves the commit instead.
        if (not isinstance(generated.get("profiles"), list)
                or not generated["profiles"]
                or not all(isinstance(item, str) and item for item in generated["profiles"])
                or not isinstance(generated.get("profiles_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", generated["profiles_sha256"])
                or generated["profiles_sha256"] != FleetSource(
                    root, profiles_dir, published_supply, True).profiles_sha256):
            raise ValueError(
                "configured fleet source published supply has stale or incomplete profile metadata"
            )
        configured_commit = os.environ.get("TARTCI_FLEET_SOURCE_COMMIT", "").strip()
        if not COMMIT.fullmatch(configured_commit):
            raise ValueError(
                "configured fleet source requires TARTCI_FLEET_SOURCE_COMMIT "
                "to be a 40-character commit"
            )
        configured_repo = os.environ.get("TARTCI_FLEET_SOURCE_REPOSITORY", "").strip()
        if not configured_repo:
            raise ValueError(
                "configured fleet source requires TARTCI_FLEET_SOURCE_REPOSITORY"
            )
        problem = marker_problem(root, configured_repo, configured_commit)
        if problem:
            raise ValueError(f"configured fleet source provenance does not match environment: {problem}")
    return FleetSource(root, profiles_dir, published_supply, bool(configured_root))
