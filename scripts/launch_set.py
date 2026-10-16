#!/usr/bin/env python3
"""The signed launch set: what launcher v2 may run from one support generation.

Launcher v2 (native/macos/tartci-launcher, policy.json schema 2) is a stable
app: its bytes change only when its own code or host policy changes, so its
per-app Removable Volumes consent survives ordinary TartCI updates. What a
lane runs moves out of the app into the support generation, described by
`.tartci-launch-set.json`:

    {"schema": 1, "sequence": <monotonic per host>, "source_commit": ...,
     "tart_home": ...,
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

The launcher also keeps a floor (`~/.config/tartci/launcher-floor.json`): the
newest sequence it has run and the one before it. It refuses an older set
except that previous one. A missing floor fails open (the first run on a host
records it); an unreadable or malformed floor fails closed. A lane may not set
any PYTHON* variable: the launcher sets PYTHONDONTWRITEBYTECODE and
PYTHONNOUSERSITE itself.
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
# v2's own entrypoint and interpreter shim. The v1 `.tartci-launch` (the
# canonical wrapper bytes every v1 host checks) is not touched.
ENTRYPOINT = ".tartci-launch-v2"
SHIM = "bin/python3"
# Lane Python never runs site (-S: no site-packages .pth or sitecustomize,
# including a user-owned Homebrew site-packages), the user site (-s) or
# PYTHON* variables (-E), and writes no bytecode (-B). -I is not used: it also
# implies -P, which drops the script's own directory that sibling imports need.
SHIM_FLAGS = "-B -E -s -S"
# Paths written into the shim and entrypoint unquoted, so the launcher can
# rebuild the exact bytes: absolute, no space, quote or shell metacharacter.
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._+@/-]+$")
INTERPRETER_KEYS = ("TARTCI_PYTHON", "SHIPYARD_GHAPP_PYTHON_BINARY")
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
    # Developer ID only: the intermediate's Developer ID CA marker and the
    # leaf's Developer ID Application marker refuse Apple Development certs.
    return ('anchor apple generic'
            ' and certificate 1[field.1.2.840.113635.100.6.2.6] exists'
            ' and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
            f' and certificate leaf[subject.OU] = "{team_id}"'
            f' and identifier "{LAUNCH_SET_IDENTIFIER}"')


def shim_bytes(pin: str) -> bytes:
    """The generation's bin/python3: the pinned interpreter, isolated."""
    if not SAFE_PATH.fullmatch(pin):
        raise LaunchSetError(f"pinned interpreter is not a plain absolute path: {pin!r}")
    return f'#!/bin/sh\nexec {pin} {SHIM_FLAGS} "$@"\n'.encode()


def entrypoint_bytes(generation: Path) -> bytes:
    """v2's entrypoint: verify the support cohort through the shim, then run it."""
    root = str(generation)
    if not SAFE_PATH.fullmatch(root):
        raise LaunchSetError(f"generation path is not a plain absolute path: {root!r}")
    return ("#!/bin/bash\n"
            f"{root}/{SHIM} {root}/scripts/tartci_support_manifest.py verify "
            f"{root}/{MANIFEST_NAME} --root {root} --immutable >/dev/null || exit $?\n"
            f'exec /bin/bash {root}/tartci "$@"\n').encode()


def write_runtime(generation: Path, pin: str) -> None:
    """Write v2's entrypoint and interpreter shim into a writable generation."""
    (generation / "bin").mkdir(exist_ok=True)
    for relative, data in ((SHIM, shim_bytes(pin)), (ENTRYPOINT, entrypoint_bytes(generation))):
        path = generation / relative
        path.unlink(missing_ok=True)
        path.write_bytes(data)
        os.chmod(path, 0o555)


def build(generation: Path, lanes: dict, *, tart_home: str, sequence: int, python: str) -> dict:
    """The launch set for one generation from its manifest and rendered lanes."""
    manifest = generation / MANIFEST_NAME
    entry = generation / ENTRYPOINT
    try:
        source_commit = json.loads(manifest.read_text())["source_commit"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LaunchSetError(f"unreadable support manifest {manifest}: {exc}") from exc
    if type(sequence) is not int or sequence < 1:
        raise LaunchSetError("a launch set needs a positive integer sequence")
    shim_path = str(generation / SHIM)
    for relative, expected in ((SHIM, shim_bytes(python)), (ENTRYPOINT, entrypoint_bytes(generation))):
        try:
            actual = (generation / relative).read_bytes()
        except OSError as exc:
            raise LaunchSetError(f"{relative} is missing; write_runtime writes it") from exc
        if actual != expected:
            raise LaunchSetError(f"{relative} is not the expected bytes for this generation and pin")
    if not lanes or not all(isinstance(k, str) and LANE.fullmatch(k) for k in lanes):
        raise LaunchSetError("a launch set needs at least one valid lane id")
    for lane, row in lanes.items():
        env = (row or {}).get("environment") if isinstance(row, dict) else None
        if not isinstance(env, dict) or env.get("TART_HOME") != tart_home:
            raise LaunchSetError(f"lane {lane} does not run on the Tart store {tart_home}")
        if any(key.startswith("DYLD_") for key in env):
            raise LaunchSetError(f"lane {lane} sets a dynamic-loader variable")
        variables = sorted(key for key in env if key.startswith("PYTHON"))
        if variables:
            raise LaunchSetError(f"lane {lane} sets {variables[0]}; Python start-up variables "
                                 "are the launcher's")
        for key in INTERPRETER_KEYS:
            if key in env and env[key] not in (python, shim_path):
                raise LaunchSetError(f"lane {lane} {key} is {env[key]}, not the pinned "
                                     f"interpreter {python}")
    return {
        "schema": SCHEMA,
        "sequence": sequence,
        "source_commit": source_commit,
        "tart_home": tart_home,
        "manifest_sha256": _sha256(manifest),
        "entrypoint": {"path": ENTRYPOINT, "sha256": _sha256(entry)},
        "python": {"pin": python, "shim": {"path": SHIM, "sha256": _sha256(generation / SHIM)}},
        # Every interpreter a lane names is the shim.
        "lanes": {lane: {"environment": dict(sorted(
                      {**row["environment"],
                       **{key: shim_path for key in INTERPRETER_KEYS
                          if key in row["environment"]}}.items()))}
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
    shim = (value.get("python") or {}).get("shim") or {}
    allowed = {m.get("path") for m in members} | {entry.get("path"), shim.get("path"),
                                                  SET_NAME, SIG_NAME, MANIFEST_NAME}
    for dirpath, dirs, files in os.walk(generation):
        for name in dirs + files:
            full = Path(dirpath) / name
            relative = full.relative_to(generation).as_posix()
            if name == "__pycache__":
                return f"{relative}: a bytecode cache in the generation is never hashed"
            if full.is_symlink():
                return f"{relative} is a symlink"
            if not (full.is_dir() or full.is_file()):
                return f"{relative} is not a regular file"
            if full.is_file() and relative not in allowed:
                return f"{relative} is not in the signed manifest"
    for relative, expected in [(m.get("path"), m.get("sha256")) for m in members] + [
            (entry.get("path"), entry.get("sha256")), (shim.get("path"), shim.get("sha256"))]:
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
    note = None
    if previous and previous != str(generation):
        # The launcher refuses a set older than the last one it ran; refuse to
        # point it at one in the first place. A previous generation whose set
        # cannot be read is treated as absent (and named in `note`): it can no
        # longer serve as a rollback, and the launcher's own floor still
        # refuses a downgrade, so it must not block every later publish.
        after = json.loads((generation / SET_NAME).read_text())["sequence"]
        try:
            before = json.loads((Path(previous) / SET_NAME).read_text())["sequence"]
            if type(before) is not int:
                raise ValueError(f"sequence {before!r}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            note = f"previous generation {previous} has no readable launch set ({exc}); treated as absent"
            before = None
        if before is not None and after <= before:
            raise LaunchSetError(f"launch set sequence {after} is not after {before}, pointer unchanged")
    if note:
        previous = None
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
    return {**value, "note": note} if note else value
