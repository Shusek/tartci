#!/usr/bin/env python3
"""Typed support operations for the Shipyard queue-tick shell wrapper."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


REPO_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]+"
)
RFC3339_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


def _json_stdin() -> Any:
    return json.load(sys.stdin)


def _exact_int(value: Any, description: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{description} must be an integer")
    return value


def _load_ledger(path: Path) -> dict[str, int]:
    try:
        with path.open() as source:
            data = json.load(source)
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("invalid ledger must contain a JSON object")
    if any(type(value) is not int or value < 0 for value in data.values()):
        raise ValueError(
            "invalid ledger counters must be nonnegative integers"
        )
    return data


def _atomic_json(path: Path, value: Any, *, prefix: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=prefix, dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w") as destination:
            json.dump(value, destination, sort_keys=True)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def command_health(args: argparse.Namespace) -> None:
    path = Path(args.path)
    _atomic_json(
        path,
        {
            "schema_version": 1,
            "status": args.status,
            "reason": args.reason,
            "host": args.host,
            "observed_at": args.observed_at,
        },
        prefix=".queue-tick-health.",
    )


def command_validate_tunables(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[0-9]+", args.fresh) or not (
        0 <= int(args.fresh) <= 604800
    ):
        raise ValueError("heartbeat freshness must be 0..604800")
    if not re.fullmatch(r"[0-9]+", args.threshold) or not (
        1 <= int(args.threshold) <= 100000
    ):
        raise ValueError("invalid threshold must be 1..100000")


def command_ledger_validate(args: argparse.Namespace) -> None:
    path = Path(args.path)
    data = _load_ledger(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".queue-tick-ledger-probe.", dir=path.parent
    )
    temp = Path(temp_name)
    published = Path(f"{temp}.published")
    try:
        with os.fdopen(fd, "w") as destination:
            json.dump(data, destination, sort_keys=True)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temp, published)
    finally:
        for candidate in (temp, published):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


def command_ledger_update(args: argparse.Namespace) -> None:
    path = Path(args.path)
    data = _load_ledger(path)
    key = f"{args.repo}#{args.pr}"
    if args.outcome == "not_found":
        data[key] = data.get(key, 0) + 1
    else:
        data.pop(key, None)
    _atomic_json(path, data, prefix=".queue-tick-invalid.")
    print(int(data.get(key, 0)))


def _version(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    return tuple(map(int, match.groups())) if match else None


def command_version_compatible(args: argparse.Namespace) -> None:
    installed = _version(args.installed)
    required = _version(args.required)
    print(
        "1"
        if installed is not None
        and required is not None
        and installed >= required
        else "0"
    )


def command_control_flags(_: argparse.Namespace) -> None:
    value = _json_stdin()
    if not isinstance(value, dict):
        raise ValueError("control must be an object")
    held = value.get("held")
    if type(held) is not bool:
        raise ValueError("control hold must be an exact JSON boolean")
    print(int(held))


def _timestamp_epoch(value: Any, field: str) -> int:
    if value is None or value == "":
        return 0
    if not isinstance(value, str) or not RFC3339_PATTERN.fullmatch(value):
        raise ValueError(f"state {field} must be empty or RFC3339")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(
            f"state {field} must be empty or RFC3339"
        ) from error
    if parsed.tzinfo is None:
        raise ValueError(f"state {field} must carry an RFC3339 offset")
    return int(parsed.timestamp())


def command_state_rows(args: argparse.Namespace) -> None:
    with Path(args.path).open() as source:
        data = json.load(source)
    if not isinstance(data, dict) or not isinstance(data.get("states"), list):
        raise ValueError("expected object with states array")
    rows: list[tuple[str, str, str]] = []
    for state in data["states"]:
        if not isinstance(state, dict):
            raise ValueError("state entry must be an object")
        pr = state.get("pr")
        if isinstance(pr, bool) or not (
            isinstance(pr, int)
            and pr > 0
            or isinstance(pr, str)
            and re.fullmatch(r"[1-9][0-9]*", pr)
        ):
            raise ValueError("state pr must be a positive integer")
        repo = state.get("repo")
        if not isinstance(repo, str) or not REPO_PATTERN.fullmatch(repo):
            raise ValueError(
                "state repo must be a canonical owner/name slug"
            )
        runs = state.get("dispatched_runs")
        if runs is None:
            runs = []
        if not isinstance(runs, list) or any(
            not isinstance(run, dict) for run in runs
        ):
            raise ValueError(
                "state dispatched_runs must be an array of objects"
            )
        heartbeats = [
            _timestamp_epoch(run.get("last_heartbeat_at"), "heartbeat")
            for run in runs
        ]
        timestamps = [
            _timestamp_epoch(state.get(field), field)
            for field in ("updated_at", "created_at")
        ]
        fresh = max([*heartbeats, *timestamps], default=0)
        rows.append((str(pr), repo, str(fresh)))
    for row in rows:
        print("\t".join(row))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    health = commands.add_parser("health")
    health.add_argument("path")
    health.add_argument("status")
    health.add_argument("reason")
    health.add_argument("host")
    health.add_argument("observed_at")
    health.set_defaults(func=command_health)

    tunables = commands.add_parser("validate-tunables")
    tunables.add_argument("fresh")
    tunables.add_argument("threshold")
    tunables.set_defaults(func=command_validate_tunables)

    ledger_validate = commands.add_parser("ledger-validate")
    ledger_validate.add_argument("path")
    ledger_validate.set_defaults(func=command_ledger_validate)

    ledger_update = commands.add_parser("ledger-update")
    ledger_update.add_argument("path")
    ledger_update.add_argument("repo")
    ledger_update.add_argument("pr")
    ledger_update.add_argument("outcome", choices=("not_found", "reset"))
    ledger_update.set_defaults(func=command_ledger_update)

    version = commands.add_parser("version-compatible")
    version.add_argument("installed")
    version.add_argument("required")
    version.set_defaults(func=command_version_compatible)

    control = commands.add_parser("control-flags")
    control.set_defaults(func=command_control_flags)

    rows = commands.add_parser("state-rows")
    rows.add_argument("path")
    rows.set_defaults(func=command_state_rows)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
