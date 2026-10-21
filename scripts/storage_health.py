#!/usr/bin/env python3
"""Bounded, fail-closed storage admission probe for TartCI macOS hosts."""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from python_child import child_python

HEALTHY, DEGRADED, QUARANTINED = 0, 10, 20
TERMINATE_GRACE_SECONDS = 0.5


def default_state(path: Path) -> Path:
    host = os.uname().nodename.split(".", 1)[0] or "host"
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:12]
    return Path.home() / ".tartci" / "state" / f"storage-health-{host}-{digest}.json"


def load_state(path: Path, target: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise ValueError("schema")
        if value.get("path") != str(target):
            raise ValueError("path")
        if value.get("status") not in {"healthy", "degraded", "quarantined"}:
            raise ValueError("status")
        for key in ("failure_streak", "healthy_streak"):
            if not isinstance(value.get(key), int) or value[key] < 0:
                raise ValueError(key)
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        return {"state_unreadable": True, "status": "quarantined"}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            try:
                os.fsync(directory)
            except OSError as exc:
                if exc.errno != errno.EINVAL:
                    raise
        finally:
            os.close(directory)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def probe(path: Path) -> tuple[bool, str, float]:
    """Inspect the store directory without changing its contents.

    The storage admission check is deliberately metadata-only.  Creating a
    temporary file, fsyncing it, or unlinking it would turn a diagnostic gate
    into a write test against the potentially wedged external volume and
    would violate the read-only recovery contract.  ``open``/``fstat`` still
    exercise the APFS lookup path that can block, and the caller runs this
    function in a bounded child process.
    """
    started = time.monotonic()
    fd = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(str(path), flags)
        try:
            stat = os.fstat(fd)
        finally:
            os.close(fd)
            fd = -1
        if not stat:
            raise OSError("directory metadata unavailable")
        return True, "directory_metadata_ok", time.monotonic() - started
    except FileNotFoundError:
        return False, "path_missing", time.monotonic() - started
    except PermissionError:
        return False, "permission_denied", time.monotonic() - started
    except InterruptedError:
        return False, "interrupted_system_call", time.monotonic() - started
    except OSError as exc:
        return False, f"os_error_{exc.errno or 'unknown'}", time.monotonic() - started
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _group_exists(pgid: int) -> bool:
    """Return whether a private process group still has a member."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # We own the group leader; permission failure still means the group is
        # present and must not be treated as safely reaped.
        return True
    return True


def reap_after_kill(child: subprocess.Popen[object]) -> bool:
    """Kill and boundedly reap a child process group.

    The probe worker can itself have a probe child blocked in a filesystem
    syscall. Killing only the worker leaves that child holding descriptors and
    can turn a bounded supervisor into an orphan leak. Every caller starts a
    private session, so kill the complete process group and verify that it has
    disappeared before returning.
    """
    pgid = child.pid
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # The leader may have exited between the timeout and this call. The
        # direct fallback is harmless on platforms without usable groups.
        try:
            child.kill()
        except OSError:
            pass
    try:
        child.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except OSError:
            pass
        try:
            child.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    # A successfully reaped leader can still have a descendant in the group.
    # Give normal signal delivery a bounded window, then make one final group
    # kill. A D-state process can remain unreapable; report that to the caller
    # while preserving the supervisor's bounded return.
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while _group_exists(pgid) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    return not _group_exists(pgid)


def run_probe_bounded(path: Path, timeout: float) -> tuple[bool, str, float]:
    child = subprocess.Popen(
        [child_python(), str(Path(__file__).resolve()), "--probe-only", "--path", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True,
    )
    try:
        stdout, _ = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Do not wait for a filesystem syscall that is stuck in the kernel. The
        # supervisor remains bounded; any orphan is harmless and holds no Tart VM.
        child.kill()
        reaped = reap_after_kill(child)
        if child.stdout is not None:
            child.stdout.close()
        return False, "probe_timeout" if reaped else "probe_timeout_unreaped", timeout
    try:
        payload = json.loads(stdout)
        return bool(payload["ok"]), str(payload["reason"]), float(payload["elapsed_seconds"])
    except (ValueError, TypeError, KeyError):
        return False, "probe_failed" if child.returncode else "probe_malformed", 0.0


def run_worker(args: argparse.Namespace) -> int:
    path = Path(args.path)
    state_path = Path(args.state) if args.state else default_state(path)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    quarantine_marker = Path(str(state_path) + ".quarantine")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            previous = load_state(state_path, path)
            if not previous and quarantine_marker.exists():
                previous = {"status": "quarantined", "failure_streak": args.failure_threshold, "healthy_streak": 0}
            if previous.get("state_unreadable"):
                result = {"schema": 1, "status": "quarantined", "reason": "health_state_unreadable", "path": str(path), "state": str(state_path), "failure_streak": args.failure_threshold, "healthy_streak": 0}
                print(json.dumps(result, sort_keys=True))
                return QUARANTINED
            ok, reason, elapsed = run_probe_bounded(path, args.timeout)
            failures = int(previous.get("failure_streak", 0))
            healthy = int(previous.get("healthy_streak", 0))
            prior_status = str(previous.get("status", "healthy"))
            if ok:
                failures, healthy = 0, healthy + 1
                status = "healthy" if prior_status != "quarantined" or healthy >= args.recovery_threshold else "quarantined"
                if prior_status == "quarantined" and healthy < args.recovery_threshold:
                    reason = "recovery_check_pending"
            else:
                failures, healthy = failures + 1, 0
                status = "quarantined" if failures >= args.failure_threshold or prior_status == "quarantined" else "degraded"
            result = {"schema": 1, "status": status, "reason": reason, "path": str(path), "state": str(state_path), "failure_streak": failures, "healthy_streak": healthy, "elapsed_seconds": round(elapsed, 3), "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            try:
                save_state(state_path, result)
                if status == "quarantined":
                    quarantine_marker.touch(exist_ok=True)
                elif quarantine_marker.exists():
                    quarantine_marker.unlink()
            except OSError:
                result.update(status="quarantined", reason="health_state_write_failed")
                print(json.dumps(result, sort_keys=True))
                return QUARANTINED
            print(json.dumps(result, sort_keys=True))
            return HEALTHY if status == "healthy" else DEGRADED if status == "degraded" else QUARANTINED
    except OSError:
        print(json.dumps({"schema": 1, "status": "quarantined", "reason": "health_state_lock_failed", "path": str(path), "state": str(state_path), "failure_streak": args.failure_threshold, "healthy_streak": 0}, sort_keys=True))
        return QUARANTINED


def run(args: argparse.Namespace) -> int:
    command = [child_python(), str(Path(__file__).resolve()), "--storage-health-worker"] + sys.argv[1:]
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=args.timeout + 2.0)
    except subprocess.TimeoutExpired:
        reap_after_kill(child)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()
        print(json.dumps({"schema": 1, "status": "quarantined", "reason": "health_supervisor_timeout", "path": args.path, "state": args.state or str(default_state(Path(args.path))), "failure_streak": args.failure_threshold, "healthy_streak": 0}, sort_keys=True))
        return QUARANTINED
    if child.returncode in (HEALTHY, DEGRADED, QUARANTINED) and stdout.strip():
        print(stdout, end="")
        return child.returncode
    if stderr.strip():
        print(stderr, file=sys.stderr, end="")
    print(json.dumps({"schema": 1, "status": "quarantined", "reason": "health_worker_failed", "path": args.path, "state": args.state or str(default_state(Path(args.path))), "failure_streak": args.failure_threshold, "healthy_streak": 0}, sort_keys=True))
    return QUARANTINED


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    parser.add_argument("--state", default="")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--failure-threshold", type=int, default=2)
    parser.add_argument("--recovery-threshold", type=int, default=3)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--storage-health-worker", action="store_true")
    args = parser.parse_args()
    if args.probe_only:
        ok, reason, elapsed = probe(Path(args.path))
        print(json.dumps({"ok": ok, "reason": reason, "elapsed_seconds": elapsed}))
        return 0 if ok else 1
    if not (0 < args.timeout <= 60) or not (1 <= args.failure_threshold <= 10) or not (1 <= args.recovery_threshold <= 20):
        print(json.dumps({"schema": 1, "status": "quarantined", "reason": "health_configuration_invalid", "path": args.path, "state": args.state or str(default_state(Path(args.path))), "failure_streak": 0, "healthy_streak": 0}, sort_keys=True))
        return QUARANTINED
    return run_worker(args) if args.storage_health_worker else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
