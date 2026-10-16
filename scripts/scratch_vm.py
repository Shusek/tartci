#!/usr/bin/env python3
"""Test VMs for agents on a fleet host, deleted on a TTL.

On 2026-10-16 an agent's manual repro VM ran on m1 for 11 h and nobody knew
whose it was or when it could go. A scratch VM is the sanctioned way for an
agent (or a person) to make a test VM on a fleet host: it has an owner and a
time to live, it is recorded where the watchdog reads it, and the watchdog
stops and deletes it when the TTL ends, after a warning 30 minutes before.

    tartci scratch-vm create --owner <agent> --ttl <hours> [--from IMAGE] [--name NAME]
    tartci scratch-vm list [--json]
    tartci scratch-vm extend NAME --ttl <hours>      # a new TTL from now
    tartci scratch-vm delete NAME
    tartci scratch-vm enforce                        # what the watchdog runs

The VM lives in the host's VM store (TART_HOME, else the fleet profile's
host.tart_home, else ~/.tart), so it is counted by the same disk and VM-cap
checks as the lanes. Its record is ~/.tartci/state/scratch-vms/<name>.json.
Only VMs with such a record are ever deleted here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MAX_TTL_HOURS = 72.0
WARN_BEFORE_SECONDS = 30 * 60
DEFAULT_IMAGE = "ghcr.io/cirruslabs/ubuntu:latest"
OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
NAME_RE = re.compile(r"^scratch-[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")

Runner = Callable[[list, dict], "subprocess.CompletedProcess[str]"]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def records_dir() -> Path:
    base = os.environ.get("TARTCI_HOME") or os.path.expanduser("~/.tartci")
    return Path(base) / "state" / "scratch-vms"


def store() -> Path:
    import tart_home  # noqa: PLC0415 - sibling module; one resolution order
    return Path(tart_home.resolve()["path"])


def _tart() -> str:
    return shutil.which("tart") or "/opt/homebrew/bin/tart"


def run_tart(argv: list, env_store: dict) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([_tart(), *argv], capture_output=True, text=True, timeout=600,
                          check=False, env={**os.environ, **env_store})


def _ttl(value: str) -> float:
    hours = float(value)
    if not 0 < hours <= MAX_TTL_HOURS:
        raise argparse.ArgumentTypeError(f"--ttl must be more than 0 and at most "
                                         f"{MAX_TTL_HOURS:g} hours")
    return hours


def read(name: str) -> Optional[dict]:
    try:
        value = json.loads((records_dir() / f"{name}.json").read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write(record: dict) -> None:
    path = records_dir() / f"{record['name']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def records() -> list[dict]:
    out = []
    for path in sorted(records_dir().glob("scratch-*.json")):
        value = read(path.stem)
        if value and NAME_RE.match(str(value.get("name") or "")):
            out.append(value)
    return out


def create(owner: str, ttl_hours: float, image: str, name: Optional[str], now: float,
           tart: Runner = run_tart) -> tuple[int, str]:
    if not OWNER_RE.match(owner):
        return 2, f"--owner must match {OWNER_RE.pattern}: {owner}"
    name = name or f"scratch-{owner}-{time.strftime('%Y%m%d%H%M%S', time.gmtime(now))}"
    if not NAME_RE.match(name):
        return 2, f"--name must start with scratch- and match {NAME_RE.pattern}: {name}"
    if read(name):
        return 2, f"a scratch VM named {name} is already recorded"
    where = str(store())
    record = {"name": name, "owner": owner, "store": where, "from": image,
              "created_at": now, "expires_at": now + ttl_hours * 3600, "warned_at": None}
    # Recorded before the clone: a crash mid-clone still leaves the VM reapable.
    write(record)
    result = tart(["clone", image, name], {"TART_HOME": where})
    if result.returncode != 0:
        (records_dir() / f"{name}.json").unlink(missing_ok=True)
        return 1, f"tart clone {image} {name} failed: {(result.stderr or result.stdout).strip()}"
    return 0, (f"scratch VM {name} (owner {owner}) in {where}, expires "
               f"{_iso(record['expires_at'])}; run it with `TART_HOME={where} tart run {name}`. "
               f"The watchdog warns 30 min before and then stops and deletes it.")


def extend(name: str, ttl_hours: float, now: float) -> tuple[int, str]:
    record = read(name)
    if not record:
        return 2, f"no scratch VM named {name}"
    record["expires_at"], record["warned_at"] = now + ttl_hours * 3600, None
    write(record)
    return 0, f"scratch VM {name} now expires {_iso(record['expires_at'])}"


def remove(record: dict, tart: Runner = run_tart) -> tuple[bool, str]:
    """Stop and delete the VM, then its record. False keeps the record for a retry."""
    env = {"TART_HOME": record["store"]}
    tart(["stop", record["name"]], env)
    gone = tart(["delete", record["name"]], env)
    text = (gone.stderr or gone.stdout).strip()
    if gone.returncode != 0 and "does not exist" not in text.lower():
        return False, f"tart delete {record['name']} failed: {text[:200]}"
    (records_dir() / f"{record['name']}.json").unlink(missing_ok=True)
    return True, f"deleted scratch VM {record['name']} (owner {record['owner']})"


def enforce(now: float, tart: Runner = run_tart) -> list[str]:
    """Warn at TTL-30m, then stop and delete at the TTL. One line per action."""
    lines = []
    for record in records():
        expires = float(record.get("expires_at") or 0)
        if now >= expires:
            ok, text = remove(record, tart)
            lines.append(f"{_iso(now)} launchd-watchdog: {'' if ok else 'WARN '}scratch-vm: "
                         f"{text} (TTL ended {_iso(expires)})")
        elif now >= expires - WARN_BEFORE_SECONDS and not record.get("warned_at"):
            record["warned_at"] = now
            write(record)
            lines.append(f"{_iso(now)} launchd-watchdog: WARN scratch-vm: {record['name']} "
                         f"(owner {record['owner']}) is stopped and deleted at {_iso(expires)}; "
                         f"keep it with `tartci scratch-vm extend {record['name']} --ttl <hours>`")
    return lines


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create")
    c.add_argument("--owner", required=True)
    c.add_argument("--ttl", required=True, type=_ttl, help="hours, at most 72")
    c.add_argument("--from", dest="image", default=DEFAULT_IMAGE)
    c.add_argument("--name")
    lst = sub.add_parser("list")
    lst.add_argument("--json", action="store_true")
    e = sub.add_parser("extend")
    e.add_argument("name")
    e.add_argument("--ttl", required=True, type=_ttl)
    d = sub.add_parser("delete")
    d.add_argument("name")
    sub.add_parser("enforce")
    args = parser.parse_args(argv)
    now = time.time()
    if args.command == "create":
        rc, text = create(args.owner, args.ttl, args.image, args.name, now)
    elif args.command == "extend":
        rc, text = extend(args.name, args.ttl, now)
    elif args.command == "delete":
        record = read(args.name)
        if not record:
            rc, text = 2, f"no scratch VM named {args.name}"
        else:
            ok, text = remove(record)
            rc = 0 if ok else 1
    elif args.command == "list":
        rows = records()
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
            return 0
        for row in rows:
            left = (float(row["expires_at"]) - now) / 60
            print(f"{row['name']}\towner={row['owner']}\texpires={_iso(row['expires_at'])}"
                  f"\t({left:.0f} min left)\tstore={row['store']}")
        return 0
    else:
        for line in enforce(now):
            print(line)
        return 0
    print(text, file=sys.stderr if rc else sys.stdout)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
