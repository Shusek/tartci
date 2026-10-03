#!/usr/bin/env python3
"""Per-job ccache write layers, promoted into the shared cache only on green.

The problem this solves: every macOS guest mounted the host ccache read-write,
so a job torn down mid-build (a Tart hang, a timeout, a cancel) could leave
half-written direct-mode manifests that every later job then trusted. With
write isolation on, a guest reads the shared cache but writes only to its own
job layer, and this module is the host side that decides what happens to that
layer after the job:

  settle   decide the verdict (green or red) from the supervisor's own job
           evidence and move the layer to ``green/`` or ``discard/``;
  process  for a green layer, wait until the VM is provably gone, then promote
           each entry into ``shared/``; for a discarded layer, delete it;
  sweep    reclaim what an interrupted supervisor left behind: an active layer
           whose owner is dead is discarded (never promoted), a green layer
           whose promoter died is promoted again (promotion is idempotent);
  trim     keep ``shared/`` under a size cap, oldest entries first.

Layout under the layers root (inside the host ccache directory the guest
mounts, so every move is a same-filesystem rename):

  shared/<k0k1>/<rest>      ccache ``file:`` remote-storage layout, read by guests
  jobs/<vm>/local/          the guest's CCACHE_DIR (stats only: remote_only)
  jobs/<vm>/remote/         the guest's writable remote storage
  green/<vm>/  discard/<vm>/

Host-only state (never mounted into a guest) lives in a separate state
directory: owner pids, per-layer locks, and ``audit.jsonl``.

Promotion rules (the tests in test_ccache_layer.py pin each one):
  * only a green verdict promotes; red, cancelled, timed-out, orphaned or
    unproved-teardown layers are deleted without touching ``shared/``;
  * a result entry is published with a hard link, which fails if the key
    already exists, so an existing entry is never overwritten;
  * a manifest may replace an existing manifest only when it is strictly newer,
    under a lock, through a temp file and an atomic rename;
  * anything that is not a regular file with a ccache entry header in a
    well-formed key path is rejected, and nothing is ever followed through a
    symlink.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO

ENTRY_MAGIC = b"\xcc\xac"
ENTRY_HEADER_MIN = 15
ENTRY_TYPE_RESULT = 0
ENTRY_TYPE_MANIFEST = 1
KEY_DIR = re.compile(r"^[0-9a-z]{2}$")
KEY_NAME = re.compile(r"^[0-9a-z]{8,128}$")
VM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
GREEN_RESULTS = ("Succeeded", "SucceededWithIssues")
TMP_PREFIX = ".tartci-tmp-"
TRIM_STAMP = ".tartci-last-trim"
DEFAULT_VM_GONE_TIMEOUT = 1800.0
DEFAULT_TRIM_INTERVAL = 3600.0
STALE_TMP_SECONDS = 3600.0


# ── Verdict ────────────────────────────────────────────────────────────────


def runner_log_result(path: Path | None) -> str | None:
    """The Actions runner's own ``Job … completed with result: X`` value."""
    if path is None:
        return None
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    result = None
    for line in text.splitlines():
        marker = "completed with result: "
        if ": Job " in line and marker in line:
            result = line.rsplit(marker, 1)[1].strip()
    return result


def verdict(runner_rc: int, capture_status: str, receipt: str, quarantine: str,
            local_result: str | None) -> tuple[str, str]:
    """(``green``|``red``, reason). Anything short of positive proof is red."""
    if runner_rc != 0:
        return "red", f"runner_rc={runner_rc}"
    if quarantine not in ("", "none"):
        return "red", f"quarantine={quarantine}"
    if local_result is None:
        return "red", "no_completion_line"
    if local_result not in GREEN_RESULTS:
        return "red", f"result={local_result}"
    if capture_status == "terminal":
        try:
            conclusion = json.loads(receipt or "{}").get("conclusion")
        except (json.JSONDecodeError, AttributeError):
            return "red", "receipt_unreadable"
        if conclusion != "success":
            return "red", f"conclusion={conclusion}"
    return "green", f"result={local_result}"


# ── Paths, locks, audit ────────────────────────────────────────────────────


class Layout:
    def __init__(self, root: Path, state: Path) -> None:
        self.root = root
        self.state = state
        self.shared = root / "shared"
        self.jobs = root / "jobs"
        self.green = root / "green"
        self.discard = root / "discard"
        self.owners = state / "owners"
        self.locks = state / "locks"
        self.promotions = state / "promotions"
        self.audit = state / "audit.jsonl"

    def ensure(self) -> None:
        for path in (self.shared, self.jobs, self.green, self.discard,
                     self.owners, self.locks, self.promotions):
            path.mkdir(parents=True, exist_ok=True)


def check_vm(vm: str) -> str:
    if not VM_NAME.fullmatch(vm):
        raise ValueError(f"invalid VM name: {vm!r}")
    return vm


def audit(layout: Layout, event: str, **fields: object) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "event": event, **fields}
    line = (json.dumps(record, sort_keys=True) + "\n").encode()
    layout.state.mkdir(parents=True, exist_ok=True)
    fd = os.open(layout.audit, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


@contextlib.contextmanager
def flock(path: Path, blocking: bool = True) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def remove_tree(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return True
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


# ── Attach / settle ────────────────────────────────────────────────────────


def attach(layout: Layout, vm: str, owner_pid: int) -> Path:
    """Create an empty job layer and record which supervisor owns it."""
    check_vm(vm)
    layout.ensure()
    layer = layout.jobs / vm
    if layer.exists() or layer.is_symlink():
        # A reused name can only be residue from a supervisor that died before
        # settling it; nothing in it has a verdict, so it is never promoted.
        discard_path = unique_target(layout.discard, vm)
        os.rename(layer, discard_path)
        audit(layout, "discard", vm=vm, reason="stale_layer_on_attach")
        remove_tree(discard_path)
    # Owner first: a concurrent sweep that can see the layer can see its owner.
    (layout.owners / vm).write_text(f"{owner_pid}\n")
    (layer / "local").mkdir(parents=True)
    (layer / "remote").mkdir()
    audit(layout, "attach", vm=vm, owner_pid=owner_pid)
    return layer


def unique_target(parent: Path, vm: str) -> Path:
    target = parent / vm
    if not target.exists():
        return target
    return parent / f"{vm}.{uuid.uuid4().hex[:8]}"


def settle(layout: Layout, vm: str, runner_rc: int, capture_status: str,
           receipt: str, quarantine: str, runner_log: Path | None,
           run_id: str = "", job_id: str = "") -> tuple[str, str, Path | None]:
    """Record the verdict and move the job layer out of ``jobs/``."""
    check_vm(vm)
    layout.ensure()
    local_result = runner_log_result(runner_log)
    decision, reason = verdict(runner_rc, capture_status, receipt, quarantine,
                               local_result)
    layer = layout.jobs / vm
    if not layer.is_dir() or layer.is_symlink():
        audit(layout, "settle", vm=vm, verdict=decision, reason=reason,
              layer="missing", run_id=run_id, job_id=job_id)
        return decision, reason, None
    target = unique_target(layout.green if decision == "green" else layout.discard, vm)
    os.rename(layer, target)
    with contextlib.suppress(OSError):
        (layout.owners / vm).unlink()
    if decision == "green":
        (layout.state / "green-meta").mkdir(parents=True, exist_ok=True)
        (layout.state / "green-meta" / f"{target.name}.json").write_text(
            json.dumps({"vm": vm, "run_id": run_id, "job_id": job_id,
                        "reason": reason}))
    audit(layout, "settle", vm=vm, verdict=decision, reason=reason,
          layer=target.name, run_id=run_id, job_id=job_id)
    return decision, reason, target


# ── Promotion ──────────────────────────────────────────────────────────────


@contextlib.contextmanager
def directory_fd(path: Path, *, create: bool = False) -> Iterator[int]:
    """Pin a directory without following symlinks in any guest-controlled part.

    Checking/resolving a path before reopening it leaves a race. Walk from the
    filesystem root using openat instead, and retain the fd for all operations.
    macOS's fixed, root-owned /var, /tmp and /etc aliases are the only exception.
    """
    parts = Path(os.path.abspath(path)).parts[1:]
    if sys.platform == "darwin" and parts and parts[0] in ("var", "tmp", "etc"):
        parts = ("private", *parts)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for name in parts:
            if create:
                try:
                    os.mkdir(name, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def regular_at(parent: int, name: str) -> int:
    """Open a regular file relative to a pinned parent, without blocking on FIFOs."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, "not a regular file", name)
    return fd


def open_regular(path: Path) -> int:
    """Open a guest-written file without following leaf or ancestor symlinks."""
    with directory_fd(path.parent) as parent:
        return regular_at(parent, path.name)


def entry_type(path: Path) -> int | None:
    """The ccache entry type byte, or None when the file is not an entry."""
    try:
        with os.fdopen(open_regular(path), "rb") as handle:
            header = handle.read(ENTRY_HEADER_MIN)
    except OSError:
        return None
    if len(header) < ENTRY_HEADER_MIN or header[:2] != ENTRY_MAGIC:
        return None
    return header[3]


def iter_layer_entries(remote: Path) -> Iterator[tuple[str, str, Path]]:
    """(key-dir, name, path) for every candidate file; rejects are yielded too."""
    try:
        with directory_fd(remote) as parent:
            with os.scandir(parent) as scan:
                names = sorted(item.name for item in scan)
            for name in names:
                if name == "CACHEDIR.TAG":
                    continue
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=parent)
                except OSError:
                    yield name, "", remote / name
                    continue
                try:
                    with os.scandir(child) as scan:
                        entries = sorted(item.name for item in scan)
                    for item in entries:
                        yield name, item, remote / name / item
                finally:
                    os.close(child)
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
            raise
        yield "", "", remote


def link_regular_at(src_fd: int, src_parent: int, src_name: str,
                    dest_parent: int, dest_name: str) -> None:
    """Link within pinned directories and reject a leaf changed since opening."""
    os.link(src_name, dest_name, src_dir_fd=src_parent, dst_dir_fd=dest_parent,
            follow_symlinks=False)
    source = os.fstat(src_fd)
    linked = os.stat(dest_name, dir_fd=dest_parent, follow_symlinks=False)
    if (not stat.S_ISREG(linked.st_mode)
            or (source.st_dev, source.st_ino) != (linked.st_dev, linked.st_ino)):
        os.unlink(dest_name, dir_fd=dest_parent)
        raise OSError(errno.EINVAL, "entry changed while linking", src_name)


def link_regular(src: Path, dest: Path) -> None:
    """Hard-link a regular file without following any part of either path."""
    with directory_fd(src.parent) as source, directory_fd(dest.parent) as target:
        with os.fdopen(regular_at(source, src.name), "rb") as reader:
            link_regular_at(reader.fileno(), source, src.name, target, dest.name)


def publish_result(src: Path, dest: Path) -> str:
    """Hard-link a result entry into place; an existing key always wins."""
    with directory_fd(src.parent) as source, directory_fd(dest.parent) as target:
        with os.fdopen(regular_at(source, src.name), "rb") as reader:
            try:
                link_regular_at(reader.fileno(), source, src.name, target, dest.name)
                return "promoted"
            except FileExistsError:
                return "existing"
            except OSError as exc:
                if exc.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EMLINK):
                    raise
            tmp = f"{TMP_PREFIX}{dest.name}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            try:
                copy_synced_at(reader, target, tmp)
                with os.fdopen(regular_at(target, tmp), "rb") as copied:
                    try:
                        link_regular_at(copied.fileno(), target, tmp, target, dest.name)
                    except FileExistsError:
                        return "existing"
                return "promoted"
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=target)


def copy_synced_at(reader: BinaryIO, parent: int, name: str,
                   *, preserve_times: bool = False) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=parent)
    with os.fdopen(fd, "wb") as writer:
        reader.seek(0)
        shutil.copyfileobj(reader, writer)
        writer.flush()
        if preserve_times:
            source = os.fstat(reader.fileno())
            os.utime(writer.fileno(), ns=(source.st_atime_ns, source.st_mtime_ns))
        os.fsync(writer.fileno())


def copy_synced(src: Path, dest: Path) -> None:
    with os.fdopen(open_regular(src), "rb") as reader, directory_fd(dest.parent) as target:
        copy_synced_at(reader, target, dest.name)


def publish_manifest(src: Path, dest: Path, lock_path: Path) -> str:
    """Publish a manifest, replacing an existing one only when strictly newer."""
    with flock(lock_path), directory_fd(src.parent) as source, directory_fd(dest.parent) as target:
        with os.fdopen(regular_at(source, src.name), "rb") as reader:
            try:
                existing = os.stat(dest.name, dir_fd=target, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and os.fstat(reader.fileno()).st_mtime_ns <= existing.st_mtime_ns:
                return "existing"
            tmp = f"{TMP_PREFIX}{dest.name}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            try:
                try:
                    link_regular_at(reader.fileno(), source, src.name, target, tmp)
                except OSError as exc:
                    if exc.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EMLINK):
                        raise
                    copy_synced_at(reader, target, tmp, preserve_times=True)
                os.replace(tmp, dest.name, src_dir_fd=target, dst_dir_fd=target)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=target)
            return "replaced" if existing is not None else "promoted"


def promote_layer(layout: Layout, layer: Path) -> dict[str, int | list[str]]:
    """Publish every valid entry of a green layer into ``shared/``."""
    counts: dict[str, int] = {"promoted": 0, "replaced": 0, "existing": 0,
                              "rejected": 0, "bytes": 0}
    keys: list[str] = []
    lock_path = layout.locks / "shared-manifests.lock"
    for key_dir, name, path in iter_layer_entries(layer / "remote"):
        if not KEY_DIR.fullmatch(key_dir) or not KEY_NAME.fullmatch(name):
            counts["rejected"] += 1
            continue
        try:
            info = path.lstat()
        except OSError:
            counts["rejected"] += 1
            continue
        if path.is_symlink() or not path.is_file():
            counts["rejected"] += 1
            continue
        kind = entry_type(path)
        if kind not in (ENTRY_TYPE_RESULT, ENTRY_TYPE_MANIFEST):
            counts["rejected"] += 1
            continue
        dest_dir = layout.shared / key_dir
        dest = dest_dir / name
        try:
            with directory_fd(dest_dir, create=True):
                pass
            if kind == ENTRY_TYPE_MANIFEST:
                outcome = publish_manifest(path, dest, lock_path)
            else:
                outcome = publish_result(path, dest)
        except OSError as exc:
            # Swapped for a symlink, FIFO or directory after the checks above.
            if exc.errno not in (errno.EINVAL, errno.ELOOP, errno.ENOENT, errno.EISDIR, errno.ENOTDIR):
                raise
            outcome = "rejected"
        counts[outcome] += 1
        if outcome in ("promoted", "replaced"):
            counts["bytes"] += info.st_size
            keys.append(f"{key_dir}{name}")
    return {**counts, "keys": keys}


def layer_stats(layer: Path, ccache: str | None) -> dict[str, int] | None:
    """Hit/miss counters from the job's own stats, when a host ccache exists."""
    if not ccache:
        return None
    # The layer is guest-written: read its stats, never its ccache.conf.
    env = dict(os.environ, CCACHE_DIR=str(layer / "local"), CCACHE_CONFIGPATH=os.devnull)
    try:
        output = subprocess.run([ccache, "--print-stats"], env=env, text=True,
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    wanted = {"direct_cache_hit", "preprocessed_cache_hit", "cache_miss",
              "remote_storage_hit", "remote_storage_miss", "remote_storage_error"}
    stats: dict[str, int] = {}
    for line in output.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] in wanted and parts[1].isdigit():
            stats[parts[0]] = int(parts[1])
    return stats or None


def wait_vm_gone(vm: str, is_present: Callable[[str], bool], timeout: float,
                 poll: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if not is_present(vm):
                return True
        except Exception:  # noqa: BLE001 - an unreadable inventory proves nothing
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def tart_presence(tart: str) -> Callable[[str], bool]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tart_inventory import vm_is_present  # noqa: PLC0415

    return lambda vm: vm_is_present(vm, 10.0, tart)


def process(layout: Layout, name: str, is_present: Callable[[str], bool],
            vm_gone_timeout: float = DEFAULT_VM_GONE_TIMEOUT, poll: float = 5.0,
            ccache: str | None = None, max_size: int | None = None,
            trim_interval: float = DEFAULT_TRIM_INTERVAL) -> str:
    """Finish one settled layer. Returns what happened, for tests and logs."""
    layout.ensure()
    green = layout.green / name
    discard = layout.discard / name
    with flock(layout.locks / f"{name}.lock", blocking=False) as held:
        if not held:
            return "busy"
        if discard.is_dir():
            ok = remove_tree(discard)
            audit(layout, "discard", layer=name, removed=ok)
            return "discarded"
        if not green.is_dir():
            return "missing"
        meta_path = layout.state / "green-meta" / f"{name}.json"
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            meta = {"vm": name.split(".", 1)[0]}
        vm = str(meta.get("vm") or name.split(".", 1)[0])
        if not wait_vm_gone(vm, is_present, vm_gone_timeout, poll):
            # The guest may still be able to write into this layer. Promotion
            # needs proof of no writer, so an unproved teardown discards.
            target = unique_target(layout.discard, name)
            os.rename(green, target)
            audit(layout, "discard", vm=vm, layer=name, reason="vm_still_present")
            remove_tree(target)
            with contextlib.suppress(OSError):
                meta_path.unlink()
            return "discarded"
        stats = layer_stats(green, ccache)
        result = promote_layer(layout, green)
        keys = result.pop("keys")
        if keys:
            keys_file = layout.promotions / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{name}.keys"
            keys_file.write_text("\n".join(keys) + "\n")
        audit(layout, "promote", vm=vm, layer=name, run_id=meta.get("run_id", ""),
              job_id=meta.get("job_id", ""), stats=stats, **result)
        remove_tree(green)
        with contextlib.suppress(OSError):
            meta_path.unlink()
    if max_size:
        trim(layout, max_size, trim_interval)
    return "promoted"


# ── Sweep and trim ─────────────────────────────────────────────────────────


def sweep(layout: Layout, is_present: Callable[[str], bool], **process_args: object) -> dict[str, int]:
    """Reclaim layers an interrupted supervisor or promoter left behind."""
    layout.ensure()
    counts = {"orphaned": 0, "resumed": 0, "discarded": 0}
    for layer in sorted(layout.jobs.iterdir()):
        owner_file = layout.owners / layer.name
        try:
            owner = int(owner_file.read_text().strip())
        except (OSError, ValueError):
            owner = 0
        if pid_alive(owner):
            continue
        target = unique_target(layout.discard, layer.name)
        os.rename(layer, target)
        with contextlib.suppress(OSError):
            owner_file.unlink()
        audit(layout, "discard", vm=layer.name, layer=target.name,
              reason="orphaned_active_layer", owner_pid=owner)
        counts["orphaned"] += 1
    for layer in sorted(layout.discard.iterdir()):
        if process(layout, layer.name, is_present, **process_args) == "discarded":
            counts["discarded"] += 1
    for layer in sorted(layout.green.iterdir()):
        if process(layout, layer.name, is_present, **process_args) == "promoted":
            counts["resumed"] += 1
    return counts


def trim(layout: Layout, max_size: int, interval: float = DEFAULT_TRIM_INTERVAL,
         now: float | None = None) -> dict[str, int] | None:
    """Evict the oldest shared entries until under 90% of ``max_size``."""
    now = time.time() if now is None else now
    with flock(layout.locks / "trim.lock", blocking=False) as held:
        if not held:
            return None
        with directory_fd(layout.shared) as shared:
            try:
                stamp = os.stat(TRIM_STAMP, dir_fd=shared, follow_symlinks=False)
                if stat.S_ISREG(stamp.st_mode) and interval > 0 and now - stamp.st_mtime < interval:
                    return None
            except FileNotFoundError:
                pass
            # Keep identities as well as names. Reopening an eviction directory
            # must neither follow a swapped symlink nor evict a replacement entry.
            entries: list[tuple[float, int, str, str, tuple[int, int], tuple[int, int]]] = []
            removed_tmp = 0
            total = 0
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            with os.scandir(shared) as scan:
                subdirs = sorted(item.name for item in scan)
            for sub in subdirs:
                try:
                    child = os.open(sub, flags, dir_fd=shared)
                except OSError:
                    continue
                try:
                    directory = os.fstat(child)
                    identity = (directory.st_dev, directory.st_ino)
                    with os.scandir(child) as scan:
                        for item in scan:
                            try:
                                info = item.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            if item.name.startswith(TMP_PREFIX):
                                if now - info.st_mtime > STALE_TMP_SECONDS:
                                    with contextlib.suppress(OSError):
                                        os.unlink(item.name, dir_fd=child)
                                        removed_tmp += 1
                                continue
                            if stat.S_ISREG(info.st_mode):
                                entries.append((info.st_mtime, info.st_size, sub, item.name,
                                                identity, (info.st_dev, info.st_ino)))
                                total += info.st_size
                finally:
                    os.close(child)
            evicted = 0
            if total > max_size:
                target = int(max_size * 0.9)
                for _, size, sub, name, directory_id, entry_id in sorted(entries, key=lambda row: row[0]):
                    if total <= target:
                        break
                    try:
                        child = os.open(sub, flags, dir_fd=shared)
                    except OSError:
                        continue
                    try:
                        with contextlib.suppress(OSError):
                            directory = os.fstat(child)
                            info = os.stat(name, dir_fd=child, follow_symlinks=False)
                            if ((directory.st_dev, directory.st_ino) == directory_id
                                    and (info.st_dev, info.st_ino) == entry_id):
                                os.unlink(name, dir_fd=child)
                                total -= size
                                evicted += 1
                    finally:
                        os.close(child)
            fd = os.open(TRIM_STAMP, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=shared)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(errno.EINVAL, "not a regular trim stamp", TRIM_STAMP)
                os.utime(fd, None)
            finally:
                os.close(fd)
        audit(layout, "trim", evicted=evicted, removed_tmp=removed_tmp,
              bytes_after=total, max_size=max_size)
        return {"evicted": evicted, "removed_tmp": removed_tmp, "bytes_after": total}


# ── CLI ────────────────────────────────────────────────────────────────────


def parse_size(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([KMGT])", value)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid size {value!r}; expected e.g. 40G")
    return int(match.group(1)) * 1024 ** "KMGT".index(match.group(2)) * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccache_layer")
    parser.add_argument("--root", required=True, type=Path,
                        help="layers root inside the host ccache directory")
    parser.add_argument("--state", required=True, type=Path,
                        help="host-only state directory (never mounted)")
    sub = parser.add_subparsers(dest="command", required=True)

    attach_cmd = sub.add_parser("attach")
    attach_cmd.add_argument("--vm", required=True)
    attach_cmd.add_argument("--owner-pid", required=True, type=int)

    settle_cmd = sub.add_parser("settle")
    settle_cmd.add_argument("--vm", required=True)
    settle_cmd.add_argument("--runner-rc", required=True, type=int)
    settle_cmd.add_argument("--capture-status", default="")
    settle_cmd.add_argument("--receipt", default="")
    settle_cmd.add_argument("--quarantine", default="")
    settle_cmd.add_argument("--runner-log", type=Path)
    settle_cmd.add_argument("--run-id", default="")
    settle_cmd.add_argument("--job-id", default="")

    for name in ("process", "sweep"):
        cmd = sub.add_parser(name)
        if name == "process":
            cmd.add_argument("--layer", required=True)
        cmd.add_argument("--tart", default=os.environ.get("TARTCI_CCACHE_LAYER_TART", "tart"))
        cmd.add_argument("--vm-gone-timeout", type=float, default=DEFAULT_VM_GONE_TIMEOUT)
        cmd.add_argument("--poll", type=float, default=5.0)
        cmd.add_argument("--ccache", default=shutil.which("ccache"))
        cmd.add_argument("--max-size", type=parse_size)
        cmd.add_argument("--trim-interval", type=float, default=DEFAULT_TRIM_INTERVAL)

    trim_cmd = sub.add_parser("trim")
    trim_cmd.add_argument("--max-size", required=True, type=parse_size)
    trim_cmd.add_argument("--trim-interval", type=float, default=0.0)

    args = parser.parse_args(argv)
    layout = Layout(args.root, args.state)
    try:
        if args.command == "attach":
            print(attach(layout, args.vm, args.owner_pid))
        elif args.command == "settle":
            decision, reason, target = settle(
                layout, args.vm, args.runner_rc, args.capture_status, args.receipt,
                args.quarantine, args.runner_log, args.run_id, args.job_id)
            print(f"{decision}\t{reason}\t{target.name if target else ''}")
        elif args.command in ("process", "sweep"):
            options = {"vm_gone_timeout": args.vm_gone_timeout, "poll": args.poll,
                       "ccache": args.ccache, "max_size": args.max_size,
                       "trim_interval": args.trim_interval}
            presence = tart_presence(args.tart)
            if args.command == "process":
                print(process(layout, args.layer, presence, **options))
            else:
                print(json.dumps(sweep(layout, presence, **options), sort_keys=True))
        else:
            print(json.dumps(trim(layout, args.max_size, args.trim_interval)))
    except (OSError, ValueError) as exc:
        print(f"ccache_layer: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
