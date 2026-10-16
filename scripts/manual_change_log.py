#!/usr/bin/env python3
"""Record who changed the pool or a lane: one JSON line per change.

At 05:19:03Z on 2026-10-16 something on m5, over ssh, disabled and booted out
every lane and re-enabled them 31 s later, killing two required jobs. Every
agent denied it and nothing recorded who it was. Every `tartci pool
on|off|drain|undrain` and every `tartci launchd reload` now appends a line to
~/Library/Logs/tartci/manual-changes.log (TARTCI_MANUAL_CHANGE_LOG overrides)
with what was asked, whether it was refused or forced, how the caller got
there (launchd, ssh with its client address, or a terminal) and the command
line of each parent process, so the next one names itself.

    manual_change_log.py ACTION OUTCOME [ARG...]

Standard library only; never fails the change it records.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

MAX_PARENTS = 8


def log_path() -> Path:
    return Path(os.environ.get("TARTCI_MANUAL_CHANGE_LOG") or os.path.expanduser(
        "~/Library/Logs/tartci/manual-changes.log"))


def _ps(pid: int) -> Optional[tuple[int, str]]:
    try:
        out = subprocess.run(["/bin/ps", "-o", "ppid=,command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.strip().splitlines()[0] if out.strip() else ""
    head, _, command = line.strip().partition(" ")
    if not head.isdigit():
        return None
    return int(head), command.strip()


def parents(start: int, ps: Callable[[int], Optional[tuple[int, str]]] = _ps) -> list[dict]:
    """[{pid, command}] from `start` up toward launchd, at most MAX_PARENTS."""
    chain = []
    pid = start
    while pid > 1 and len(chain) < MAX_PARENTS:
        found = ps(pid)
        if found is None:
            break
        ppid, command = found
        chain.append({"pid": pid, "command": command[:400]})
        pid = ppid
    return chain


def caller(env: dict) -> dict:
    launchd = env.get("TARTCI_LAUNCHD") == "1" or \
        str(env.get("XPC_SERVICE_NAME") or "").startswith("com.danielraffel.")
    ssh = env.get("SSH_CONNECTION") or env.get("SSH_CLIENT") or ""
    via = "launchd" if launchd else "ssh" if ssh else "terminal"
    try:
        tty = os.ttyname(0)
    except OSError:
        tty = ""
    return {"via": via, "ssh_client": ssh.split(" ")[0] if ssh else "",
            "ssh_tty": env.get("SSH_TTY", ""), "tty": tty,
            "launchd_job": env.get("XPC_SERVICE_NAME", ""), "user": env.get("USER", "")}


def record(action: str, outcome: str, args: list[str], *, env: Optional[dict] = None,
           start: Optional[int] = None, ps: Callable[[int], Optional[tuple[int, str]]] = _ps,
           now: Optional[float] = None) -> dict:
    env = dict(os.environ) if env is None else env
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time())),
           "action": action, "outcome": outcome, "args": args,
           **caller(env), "parents": parents(os.getppid() if start is None else start, ps)}
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass
    return row


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.splitlines()[-3].strip(), file=sys.stderr)
        return 2
    record(argv[0], argv[1], argv[2:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
