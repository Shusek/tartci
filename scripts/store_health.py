#!/usr/bin/env python3
"""Bounded, read-only Tart store health probe and fail-closed state machine."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any


DEFAULT_TIMEOUT_S = 10.0
DEFAULT_FAILURE_THRESHOLD = 2


def _probe_body(root: str) -> dict[str, Any]:
    path = pathlib.Path(root).expanduser()
    started = time.monotonic()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        stat = os.fstat(fd)
    finally:
        os.close(fd)
    return {
        "ok": True,
        "path": str(path),
        "device_id": str(stat.st_dev),
        "latency_ms": round((time.monotonic() - started) * 1000, 3),
        "checked_at": int(time.time()),
    }


def probe(root: str, timeout_s: float = DEFAULT_TIMEOUT_S,
          *, write: bool = True) -> dict[str, Any]:
    """Run the read-only directory probe in a killable process group."""
    if timeout_s <= 0:
        return {"ok": False, "reason": "invalid timeout", "path": root}
    code = (
        "import os,sys; p=sys.argv[1]; fd=os.open(p, os.O_RDONLY|getattr(os,'O_DIRECTORY',0)); "
        "s=os.fstat(fd); os.close(fd); print(s.st_dev)"
    )
    try:
        from bounded_subprocess import run_bounded
        completed = run_bounded([sys.executable, "-c", code, root],
                                timeout=timeout_s, operation="store_probe")
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit {completed.returncode}"
            return {"ok": False, "reason": detail, "path": root}
        return {"ok": True, "path": root,
                "device_id": completed.stdout.strip(),
                "latency_ms": None, "checked_at": int(time.time()),
                "read_only": True}
    except Exception as exc:  # noqa: BLE001 - typed failure for watchdog
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "path": root}


def next_state(previous: dict[str, Any], result: dict[str, Any],
               threshold: int = DEFAULT_FAILURE_THRESHOLD) -> dict[str, Any]:
    """Pure healthy/suspect/draining transition for deterministic tests."""
    prior_result = previous.get("last_result")
    if (result.get("ok") and isinstance(prior_result, dict)
            and prior_result.get("device_id")
            and result.get("device_id")
            and prior_result["device_id"] != result["device_id"]):
        result = {
            "ok": False,
            "reason": (f"device identity changed: {prior_result['device_id']} -> "
                       f"{result['device_id']}"),
            "path": result.get("path"),
            "device_id": result.get("device_id"),
        }
    failures = int(previous.get("consecutive_failures", 0) or 0)
    if result.get("ok"):
        return {"state": "healthy", "consecutive_failures": 0,
                "last_result": result, "drain_required": False}
    failures += 1
    state = "draining" if failures >= threshold else "suspect"
    return {"state": state, "consecutive_failures": failures,
            "last_result": result, "drain_required": state == "draining"}


def load(path: str) -> dict[str, Any]:
    try:
        with open(path) as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path: str, state: dict[str, Any]) -> None:
    destination = pathlib.Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with open(temporary, "w") as handle:
        json.dump(state, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
