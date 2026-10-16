#!/usr/bin/env python3
"""The signed launch set: what launcher v2 may run from one support generation.

Launcher v2 (native/macos/tartci-launcher, policy.json schema 2) is a stable
app: its bytes change only when its own code or host policy changes, so its
per-app Removable Volumes consent survives ordinary TartCI updates. What a
lane runs moves out of the app into the support generation, described by
`.tartci-launch-set.json`:

    {"schema": 1, "source_commit": ..., "tart_home": ...,
     "manifest_sha256": <sha256 of .tartci-support-manifest.json>,
     "entrypoint": {"path": ".tartci-launch", "sha256": ...},
     "lanes": {"<lane id>": {"environment": {...}}}}

The set carries a detached Developer ID signature (`.tartci-launch-set.sig`,
identifier LAUNCH_SET_IDENTIFIER), made through the dedicated signing
keychain only. The launcher verifies it against the team id in its own
policy.json, then the manifest digest, every member's digest and the
entrypoint's, and refuses any path that is not owned by the user or is
group- or world-writable. A pointer (`~/.config/tartci/launcher-generation.json`)
names the generation lanes run and the one a rollback returns to.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

SCHEMA = 1
SET_NAME = ".tartci-launch-set.json"
SIG_NAME = ".tartci-launch-set.sig"
MANIFEST_NAME = ".tartci-support-manifest.json"
ENTRYPOINT = ".tartci-launch"
LAUNCH_SET_IDENTIFIER = "com.danielraffel.tartci.launch-set"
POINTER_SCHEMA = 1
TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")
LANE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class LaunchSetError(ValueError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def requirement(team_id: str) -> str:
    """The code requirement a launch set's signature must satisfy."""
    if not TEAM_ID.fullmatch(team_id):
        raise LaunchSetError(f"not a ten-character Apple Team ID: {team_id!r}")
    return (f'anchor apple generic and certificate leaf[subject.OU] = "{team_id}" '
            f'and identifier "{LAUNCH_SET_IDENTIFIER}"')


def build(generation: Path, lanes: dict, *, tart_home: str) -> dict:
    """The launch set for one generation from its manifest and rendered lanes."""
    manifest = generation / MANIFEST_NAME
    entry = generation / ENTRYPOINT
    try:
        source_commit = json.loads(manifest.read_text())["source_commit"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LaunchSetError(f"unreadable support manifest {manifest}: {exc}") from exc
    if not lanes or not all(isinstance(k, str) and LANE.fullmatch(k) for k in lanes):
        raise LaunchSetError("a launch set needs at least one valid lane id")
    for lane, row in lanes.items():
        env = (row or {}).get("environment") if isinstance(row, dict) else None
        if not isinstance(env, dict) or env.get("TART_HOME") != tart_home:
            raise LaunchSetError(f"lane {lane} does not run on the Tart store {tart_home}")
        if any(key.startswith("DYLD_") for key in env):
            raise LaunchSetError(f"lane {lane} sets a dynamic-loader variable")
    return {
        "schema": SCHEMA,
        "source_commit": source_commit,
        "tart_home": tart_home,
        "manifest_sha256": _sha256(manifest),
        "entrypoint": {"path": ENTRYPOINT, "sha256": _sha256(entry)},
        "lanes": {lane: {"environment": dict(sorted(row["environment"].items()))}
                  for lane, row in sorted(lanes.items())},
    }


def write(generation: Path, value: dict) -> Path:
    path = generation / SET_NAME
    path.unlink(missing_ok=True)          # a re-publish replaces the read-only set
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(path, 0o444)
    return path


def sign(generation: Path, *, identity: str, keychain: Optional[str],
         run: Runner = subprocess.run) -> Path:
    """Detached-sign the launch set, through the dedicated keychain only."""
    if not keychain:
        raise LaunchSetError("a launch set is signed only through the dedicated signing "
                             "keychain; none was given")
    target = generation / SET_NAME
    sig = generation / SIG_NAME
    sig.unlink(missing_ok=True)           # a re-publish replaces the read-only signature
    result = run(["/usr/bin/codesign", "--force", "--sign", identity, "--keychain", keychain,
                  "--identifier", LAUNCH_SET_IDENTIFIER, "--timestamp",
                  "--detached", str(sig), str(target)],
                 capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0 or not sig.is_file():
        raise LaunchSetError(f"launch set signing failed: {(result.stderr or result.stdout).strip()}")
    os.chmod(sig, 0o444)
    return sig


def verify_signature(generation: Path, team_id: str, *, run: Runner = subprocess.run,
                     requirement_text: Optional[str] = None) -> Optional[str]:
    """Why the launch set's signature does not verify, or None."""
    result = run(["/usr/bin/codesign", "--verify", "--strict",
                  "--detached", str(generation / SIG_NAME),
                  f"-R={requirement_text or requirement(team_id)}",
                  str(generation / SET_NAME)],
                 capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip() or f"codesign exit {result.returncode}"
    return None


def trust_problem(path: Path, root: Path, uid: Optional[int] = None) -> Optional[str]:
    """Why `path`, or a directory from it up to `root`, is not trusted, or None.

    Trusted: not a symlink, owned by the user, not group- or world-writable.
    """
    uid = os.getuid() if uid is None else uid
    root = Path(root)
    current = Path(path)
    if current != root and root not in current.parents:
        return f"{current} is outside {root}"
    while True:
        try:
            info = current.lstat()
        except OSError as exc:
            return f"{current}: {exc}"
        if stat.S_ISLNK(info.st_mode):
            return f"{current} is a symlink"
        if info.st_uid != uid:
            return f"{current} is not owned by uid {uid}"
        if info.st_mode & 0o022:
            return f"{current} is group- or world-writable"
        if current == root:
            return None
        current = current.parent


def freeze(generation: Path) -> None:
    """Make every directory and file of a generation read-only for everyone."""
    for dirpath, dirs, files in os.walk(generation, topdown=False):
        for name in files:
            target = os.path.join(dirpath, name)
            if not os.path.islink(target):
                mode = stat.S_IMODE(os.lstat(target).st_mode)
                os.chmod(target, mode & ~0o222)
        for name in dirs:
            target = os.path.join(dirpath, name)
            if not os.path.islink(target):
                os.chmod(target, 0o555)
    os.chmod(generation, 0o555)


def verify_contents(generation: Path, root: Path) -> Optional[str]:
    """The launcher's content checks, in Python: manifest, members, entrypoint."""
    problem = trust_problem(generation, root)
    if problem:
        return problem
    try:
        value = json.loads((generation / SET_NAME).read_text())
        manifest_bytes = (generation / MANIFEST_NAME).read_bytes()
        members = json.loads(manifest_bytes)["members"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"unreadable launch set or manifest: {exc}"
    if hashlib.sha256(manifest_bytes).hexdigest() != value.get("manifest_sha256"):
        return "the support manifest does not match the launch set"
    entry = value.get("entrypoint") or {}
    for relative, expected in [(m.get("path"), m.get("sha256")) for m in members] + [
            (entry.get("path"), entry.get("sha256"))]:
        if (not isinstance(relative, str) or relative.startswith("/")
                or any(part in ("", ".", "..") for part in relative.split("/"))):
            return f"unsafe member path {relative!r}"
        target = generation / relative
        problem = trust_problem(target, root)
        if problem:
            return problem
        if not target.is_file() or _sha256(target) != expected:
            return f"{relative} does not match its digest"
    return None


def read_pointer(pointer: Path) -> Optional[dict]:
    try:
        value = json.loads(pointer.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema") != POINTER_SCHEMA:
        return None
    return value


def publish(generation: Path, *, pointer: Path, root: Path, team_id: str,
            identity: str, keychain: Optional[str],
            signing_ready: Callable[[], Optional[str]],
            run: Runner = subprocess.run,
            requirement_text: Optional[str] = None) -> dict:
    """Sign a generation's written launch set, freeze it, and point at it.

    Refuses before the pointer moves when the dedicated signing keychain is
    not ready, signing fails, or the signed, frozen set does not verify. The
    previous pointer target becomes `previous`, the rollback generation.
    """
    problem = signing_ready()
    if problem:
        raise LaunchSetError(f"signing keychain not ready, pointer unchanged: {problem}")
    if not (generation / SET_NAME).is_file():
        raise LaunchSetError(f"no {SET_NAME} in {generation}, pointer unchanged")
    os.chmod(generation, 0o755)       # room for the signature beside the set
    try:
        sign(generation, identity=identity, keychain=keychain, run=run)
    finally:
        freeze(generation)
    problem = (verify_signature(generation, team_id, run=run, requirement_text=requirement_text)
               or verify_contents(generation, root))
    if problem:
        raise LaunchSetError(f"launch set does not verify, pointer unchanged: {problem}")
    current = read_pointer(pointer)
    previous = current.get("generation") if current else None
    value = {"schema": POINTER_SCHEMA, "generation": str(generation),
             "previous": previous if previous != str(generation) else (current or {}).get("previous")}
    pointer.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".launcher-generation.", dir=pointer.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(value, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, pointer)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return value
