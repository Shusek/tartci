#!/usr/bin/env python3
"""Is a store volume stalled? A bounded probe for doctor and pool status.

A host can keep its lanes "loaded" while every process that touches its VM
store or repository volume hangs: one host's reclaim agent sat 300 s in
`git rev-parse` on its repository volume and showed only as "reclaim: STALE"
seven hours later. Such a stall is usually system-wide (a wedged sandbox or
TCC daemon) and does not clear on its own; a reboot clears it.

This module probes each store the installed fleet profile names, with a hard
timeout, and reports the volume, how long the oldest stuck process on it has
been hanging, the likely cause and the operator step:

  - the Tart VM store (`host.tart_home`): a directory listing, and `tart list`
    when tart is installed;
  - the repository volumes of `[reclaim]` (`repo`, `worktrees_root`):
    `git rev-parse --git-dir`.

It is read-only and runs only from `tartci doctor fleet` and `tartci pool
status`, never from a lane. A probe that does not finish is killed and never
waited for again, so a process stuck in the kernel cannot hang the caller.

States: ok, stalled, unprobed (no installed profile or no TOML parser).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional

try:
    import tomllib
except ModuleNotFoundError:  # the hosts' /usr/bin/python3 is 3.9
    tomllib = None  # type: ignore[assignment]

PROBE_TIMEOUT_S = 15
CAUSE = ("likely a system-wide sandbox or TCC stall (a wedged sandboxd or tccd): the volume "
         "itself answers nothing, and it does not clear on its own")
STEP = ("drain this host (`tartci pool drain`), let running jobs finish, then reboot it; a "
        "reboot clears it. Verify: `tartci doctor fleet` reports store_ok")

Runner = Callable[[list, float, Optional[dict]], Optional[float]]


def volume_of(path: str) -> str:
    """The mount a path lives on, as an operator would name it."""
    parts = pathlib.PurePosixPath(path).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return f"/Volumes/{parts[2]}"
    return "/"


def run_bounded(argv: list, timeout: float, env: Optional[dict] = None) -> Optional[float]:
    """Seconds the command took, or None when it did not finish in `timeout`.

    A command that does not finish is killed and abandoned: a process stuck
    in an uninterruptible wait may never exit, and waiting on it would hang
    the caller the same way.
    """
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True,
                                env={**os.environ, **(env or {})})
    except OSError:
        return time.monotonic() - started      # not runnable: not a stall
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        return None
    return time.monotonic() - started


def targets(profile: dict) -> list[dict]:
    """The probes for one installed profile: {volume, path, what, argv, env}."""
    out = []
    host = profile.get("host") or {}
    store = host.get("tart_home")
    if isinstance(store, str) and store.startswith("/"):
        out.append({"volume": volume_of(store), "path": store, "what": "VM store listing",
                    "argv": ["/bin/ls", "-1", store], "env": None})
        tart = shutil.which("tart", path=os.pathsep.join(
            [str(pathlib.Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]))
        if tart:
            # Only on an existing store (tart creates a missing TART_HOME), and
            # tested in the child: an in-process stat would hang on a stall.
            out.append({"volume": volume_of(store), "path": store, "what": "tart list",
                        "argv": ["/bin/sh", "-c", 'test -d "$TART_HOME" || exit 0; '
                                 'exec "$0" list --quiet', tart],
                        "env": {"TART_HOME": store}})
    reclaim = profile.get("reclaim") or {}
    seen = set()
    for key in ("repo", "worktrees_root"):
        path = reclaim.get(key)
        if not isinstance(path, str) or not path.startswith("/") or path in seen:
            continue
        seen.add(path)
        argv = (["/usr/bin/git", "-C", path, "rev-parse", "--git-dir"] if key == "repo"
                else ["/bin/ls", "-1", path])
        out.append({"volume": volume_of(path), "path": path,
                    "what": "git rev-parse" if key == "repo" else "worktrees listing",
                    "argv": argv, "env": None})
    return out


_ETIME = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$")


def etime_seconds(text: str) -> Optional[int]:
    match = _ETIME.match(text.strip())
    if not match:
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def stuck_processes(volumes: set, ps_output: Optional[str] = None,
                    min_age_s: float = PROBE_TIMEOUT_S) -> dict:
    """volume -> (oldest age in seconds, its command) for git or tart processes
    that name a path on that volume and have run longer than `min_age_s`."""
    if ps_output is None:
        try:
            ps_output = subprocess.run(["/bin/ps", "-Ao", "etime=,command="],
                                       capture_output=True, text=True, timeout=10,
                                       check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return {}
    oldest: dict = {}
    for line in ps_output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        age, command = etime_seconds(fields[0]), fields[1]
        name = os.path.basename(command.split()[0]) if command.split() else ""
        if age is None or age < min_age_s or name not in ("git", "tart"):
            continue
        for volume in volumes:
            if volume != "/" and volume + "/" in command:
                if volume not in oldest or age > oldest[volume][0]:
                    oldest[volume] = (age, command[:160])
    return oldest


def _age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def probe(probes: list, *, timeout: float = PROBE_TIMEOUT_S, runner: Runner = run_bounded,
          ps_output: Optional[str] = None) -> dict:
    """Run every probe at once (so the caller waits at most about `timeout`)."""
    results: list = [None] * len(probes)

    def one(index: int) -> None:
        item = probes[index]
        results[index] = runner(item["argv"], timeout, item["env"])

    threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(len(probes))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout + 5)
    stalled = [dict(item, argv=" ".join(item["argv"]))
               for item, took in zip(probes, results) if took is None]
    if not probes:
        return {"state": "unprobed", "detail": "the installed profile names no store to probe",
                "stalled": []}
    if not stalled:
        return {"state": "ok", "detail": f"{len(probes)} store probe(s) answered within "
                f"{timeout:g}s", "stalled": []}
    stuck = stuck_processes({item["volume"] for item in stalled}, ps_output)
    volumes = []
    for volume in sorted({item["volume"] for item in stalled}):
        what = ", ".join(sorted({item["what"] for item in stalled if item["volume"] == volume}))
        hanging = (f"; an older {os.path.basename(stuck[volume][1].split()[0])} on it has been "
                   f"hanging for {_age(stuck[volume][0])}" if volume in stuck else "")
        volumes.append(f"{volume}: {what} did not finish within {timeout:g}s{hanging}")
    return {"state": "stalled",
            "detail": ("store volume stalled: " + "; ".join(volumes) + f". Cause: {CAUSE}. "
                       f"Operator step: {STEP}"),
            "stalled": [{"volume": item["volume"], "path": item["path"], "what": item["what"],
                         "hanging_s": stuck.get(item["volume"], (None,))[0]}
                        for item in stalled]}


def installed_profile(home: pathlib.Path) -> pathlib.Path:
    return home / ".config" / "tartci" / "macos-fleet-profile.toml"


def status(home: Optional[pathlib.Path] = None, **kwargs: Any) -> dict:
    path = installed_profile(home or pathlib.Path.home())
    if tomllib is None:
        return {"state": "unprobed", "detail": "no TOML parser in this interpreter", "stalled": []}
    try:
        profile = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {"state": "unprobed", "detail": "no installed fleet profile", "stalled": []}
    except (OSError, ValueError) as exc:
        return {"state": "unprobed", "detail": f"installed profile unreadable: {exc}",
                "stalled": []}
    return probe(targets(profile), **kwargs)


def describe(value: dict) -> str:
    state = value.get("state")
    if state == "stalled":
        return "store: STALLED: " + str(value.get("detail"))
    if state == "ok":
        return "store: ok (" + str(value.get("detail")) + ")"
    return "store: not probed (" + str(value.get("detail")) + ")"


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=float, default=PROBE_TIMEOUT_S)
    args = parser.parse_args(argv)
    value = status(timeout=args.timeout)
    print(json.dumps(value, sort_keys=True) if args.json else describe(value))
    return 0


if __name__ == "__main__":
    sys.exit(main())
