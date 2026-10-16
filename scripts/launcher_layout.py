#!/usr/bin/env python3
"""Which launcher layout a sealed-launcher host installs: v1 or v2.

v1 seals the support tree and lanes into TartCILauncher.app; v2 is the stable
app that runs a signed launch set (launch_set.py). A host moves to v2 only by
opting in, one host at a time, with ~/.config/tartci/launcher-v2.enabled.

- The opt-in file must be a regular file owned by this user and not group- or
  world-writable; anything else refuses loudly rather than being read as
  either answer.
- The move from v1 to v2 is a launcher change like any other: the opt-in never
  bypasses self-update's acknowledgement or the Removable Volumes grant.
- A v2 host whose opt-in file disappears does not fall back to v1. Layouts
  never flip silently: going back is an explicit install with
  `--launcher-layout v1`, itself a launcher change.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Optional

OPT_IN = Path(".config/tartci/launcher-v2.enabled")
LAYOUTS = ("v1", "v2")


class LayoutError(ValueError):
    pass


def opt_in_path(home: Path) -> Path:
    return Path(home) / OPT_IN


def opted_in(home: Path) -> bool:
    """Whether the opt-in file is present and trusted; refuses an untrusted one."""
    path = opt_in_path(home)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LayoutError(f"cannot read {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LayoutError(f"{path} must be a regular file, not a link or directory; "
                          f"remove it and create it again with `touch {path}`")
    if info.st_uid != os.getuid():
        raise LayoutError(f"{path} is not owned by this user; remove it and create it again")
    if info.st_mode & 0o022:
        raise LayoutError(f"{path} is group- or world-writable; run `chmod 600 {path}`")
    return True


def installed(receipt: Optional[dict]) -> Optional[str]:
    """The layout the installed generation runs, from its install receipt."""
    helper = (receipt or {}).get("launch_helper")
    if not isinstance(helper, dict):
        return None
    return "v2" if helper.get("layout") == "v2" else "v1"


def decide(home: Path, receipt: Optional[dict], explicit: Optional[str] = None) -> str:
    """The layout an install may write, or LayoutError saying why not."""
    if explicit is not None and explicit not in LAYOUTS:
        raise LayoutError(f"--launcher-layout must be v1 or v2, not {explicit!r}")
    requested = "v2" if opted_in(home) else "v1"
    current = installed(receipt)
    if explicit == "v2" and requested != "v2":
        raise LayoutError(f"--launcher-layout v2 needs the per-host opt-in {opt_in_path(home)}")
    if explicit == "v1":
        return "v1"
    if current == "v2" and requested == "v1":
        raise LayoutError(
            f"this host runs launcher v2 but {opt_in_path(home)} is gone; a layout never flips "
            "back silently. To keep v2, restore the file (`touch` it, mode 600). To return to v1 "
            "deliberately, install with `--launcher-layout v1`: that replaces the launcher, so "
            "it needs the launcher-change acknowledgement and the Removable Volumes grant")
    return requested


def status(home: Path, receipt: Optional[dict]) -> dict:
    """What doctor and pool status report: the layout and the opt-in file."""
    try:
        opt_in = "present" if opted_in(home) else "absent"
        problem = None
    except LayoutError as exc:
        opt_in, problem = "refused", str(exc)
    current = installed(receipt)
    if problem is None and current == "v2" and opt_in == "absent":
        try:
            decide(home, receipt)
        except LayoutError as exc:
            problem = str(exc)
    return {"layout": current, "opt_in": opt_in, "opt_in_path": str(opt_in_path(home)),
            "problem": problem}


def status_text(value: dict) -> str:
    layout = value.get("layout") or "none (no launch helper installed)"
    line = f"launcher: layout {layout}, v2 opt-in {value['opt_in']} ({value['opt_in_path']})"
    if value.get("problem"):
        line += f"\n  launcher problem: {value['problem']}"
    return line


def read_receipt(path: Optional[Path]) -> Optional[dict]:
    if path is None or not path.exists():
        return None
    # An install receipt that exists but cannot be read is not "no layout
    # installed": that would let a v2 host flip back silently.
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise LayoutError(f"install receipt {path} is unreadable: {exc}") from exc


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--receipt", type=Path, default=None)
    parser.add_argument("--explicit", default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--installed", action="store_true",
                      help="print the installed layout (empty when none)")
    mode.add_argument("--status", action="store_true", help="print the layout report as JSON")
    mode.add_argument("--status-text", action="store_true", help="print the layout report as text")
    args = parser.parse_args(argv)
    try:
        receipt = read_receipt(args.receipt)
        if args.installed:
            print(installed(receipt) or "")
        elif args.status:
            print(json.dumps(status(args.home, receipt), sort_keys=True))
        elif args.status_text:
            print(status_text(status(args.home, receipt)))
        else:
            print(decide(args.home, receipt, args.explicit or None))
    except LayoutError as exc:
        print(f"launcher layout: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
