#!/usr/bin/env python3
"""Whether this host is configured to stay awake on AC power.

A Mac that sleeps when its display does takes its runners, SSH and lease store
with it: m5studio ran for days with System Settings > Energy > "Prevent
automatic sleeping when the display is off" left OFF and spent 4-6 hours a day
asleep, waking only for maintenance, while every launchd and pool view
reported it healthy.

Read from `pmset -g custom` (the configured AC Power profile), never from
`pmset -g`: the live view prints `sleep 1 (sleep prevented by ...)` while some
app holds an assertion, which reads as awake and is not. The host sleeps as
soon as that app lets go.

States:
  ok           AC `sleep` is 0: the host never sleeps on its own
  sleeps       AC `sleep` is N > 0 minutes
  unknown      no AC Power profile could be read (not macOS, or pmset failed)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


def parse_custom(text: str) -> dict[str, int]:
    """Return the integer settings of the `AC Power:` section of `pmset -g custom`."""
    section: str | None = None
    values: dict[str, int] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if not line[0].isspace():
            section = line.rstrip(":").strip()
            continue
        if section != "AC Power":
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("-").isdigit():
            values[" ".join(parts[:-1])] = int(parts[-1])
    return values


def status(custom_text: str | None = None) -> dict[str, Any]:
    if custom_text is None:
        try:
            custom_text = subprocess.run(
                ["pmset", "-g", "custom"], capture_output=True, text=True,
                timeout=10, check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            custom_text = ""
    values = parse_custom(custom_text)
    sleep = values.get("sleep")
    if sleep is None:
        return {"state": "unknown", "sleep_minutes": None}
    return {
        "state": "ok" if sleep == 0 else "sleeps",
        "sleep_minutes": sleep,
        "autorestart": values.get("autorestart"),
    }


def describe(value: dict[str, Any]) -> str:
    state = value.get("state")
    if state == "ok":
        return "power: ok (never sleeps on AC)"
    if state == "sleeps":
        return (f"power: WARN sleeps after {value['sleep_minutes']} min idle on AC; runners, SSH "
                "and leases go with it (System Settings > Energy > Prevent automatic sleeping "
                "when the display is off)")
    return "power: unknown (no AC Power profile from pmset)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="power_status")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    value = status()
    print(json.dumps(value, sort_keys=True) if args.json else describe(value))
    return 0


if __name__ == "__main__":
    sys.exit(main())
