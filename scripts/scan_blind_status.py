#!/usr/bin/env python3
"""Evaluate scan-blind streaks only for currently installed fleet lanes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

PREFIX = "com.danielraffel.tartci.tart-runner-macos-fleet."


def active_log_names(receipt: dict) -> set[str]:
    """Map receipted launch agents to their macos-fleet log basenames."""
    names: set[str] = set()
    rows = []
    for key in ("plists", "persistent_plists"):
        value = receipt.get(key)
        if isinstance(value, dict):
            rows.extend(value)
    for raw in rows:
        stem = Path(str(raw)).name.removesuffix(".plist")
        if stem.startswith(PREFIX):
            suffix = stem[len(PREFIX):]
            suffix = suffix.split(".", 1)[1] if "." in suffix else suffix
            names.add(f"macos-fleet-{suffix}.log")
        elif stem:
            # Test and legacy receipts may carry the already-short lane name.
            names.add(f"macos-fleet-{stem}.log")
    return names


def blind_problems(receipt: dict, log_paths: list[str], threshold: int) -> list[dict[str, object]]:
    active = active_log_names(receipt)
    problems: list[dict[str, object]] = []
    for raw in log_paths:
        path = Path(raw)
        if path.name not in active:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        count = 0
        for line in reversed(lines):
            if "SCAN BLIND" in line:
                count += 1
            elif line.strip():
                break
        if count >= threshold:
            problems.append({"code": "scan_blind", "label": str(path), "cycles": count})
    return problems


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: scan_blind_status.py RECEIPT THRESHOLD LOGS_FILE")
    receipt = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    logs = [line for line in Path(sys.argv[3]).read_text(encoding="utf-8").splitlines() if line]
    print(json.dumps(blind_problems(receipt, logs, int(sys.argv[2])), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
