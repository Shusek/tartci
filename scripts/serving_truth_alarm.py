#!/usr/bin/env python3
"""Rules-only serving truth alarm for the TartCI fleet.

This is an advisory monitor, not a scheduler or a host actuator.  It combines
the required queued work observed from GitHub with the lane event streams:
clone_start is intentionally not treated as a job start; job_assigned is the
local proof that work actually began.  Every trip writes an evidence receipt
and uses the existing :mod:`host_off` episode issue path for deduplication.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import host_off

WINDOW_SECONDS = 20 * 60
ADMISSION_STREAK = 3
TITLE = "[tartci] serving-truth alarm: {rule}"


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def read_events(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict) and row.get("event"):
            rows.append(row)
    return rows


def _time(row: dict[str, Any]) -> float | None:
    return _epoch(row.get("ts") or row.get("timestamp"))


def _host(row: dict[str, Any], default: str = "unknown") -> str:
    return str(row.get("host") or row.get("runner") or default)


def _job_starts(rows: Iterable[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    return [r for r in rows if start <= (_time(r) or -1) <= end and r.get("event") in {
        "job_assigned", "job_started", "in_progress",
    }]


def classify(
    events: Iterable[dict[str, Any]],
    queued: Iterable[dict[str, Any]] = (),
    *,
    now: float | None = None,
    window_seconds: int = WINDOW_SECONDS,
    admission_streak: int = ADMISSION_STREAK,
) -> list[dict[str, Any]]:
    """Return deterministic trips with their complete input evidence.

    ``queued`` contains the required queued run objects returned by the API.
    Callers may pre-filter runner classes; an object with ``required`` false
    is ignored.  No network or clock is used by the decision core.
    """
    rows = [r for r in events if isinstance(r, dict)]
    end = time.time() if now is None else float(now)
    start = end - window_seconds
    starts = _job_starts(rows, start, end)
    trips: list[dict[str, Any]] = []

    required = [q for q in queued if q.get("required", True) is not False]
    queued_times = [_epoch(q.get("created_at") or q.get("queued_at")) for q in required]
    queued_times = [t for t in queued_times if t is not None]
    if required and queued_times and min(queued_times) <= start and not starts:
        trips.append({
            "rule": "queued_required_no_job_start",
            "host": "fleet",
            "since": dt.datetime.fromtimestamp(min(queued_times), dt.timezone.utc).isoformat(),
            "evidence": {"queued_required": required, "window_start": start,
                         "window_end": end, "job_starts": starts},
        })

    by_host: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        ts = _time(row)
        if ts is None or not start <= ts <= end:
            continue
        if row.get("event") in {"admission_error", "admission_precheck_error", "assignment_scan_error"}:
            by_host.setdefault(_host(row), []).append(row)
    for host, errors in by_host.items():
        errors.sort(key=lambda r: _time(r) or 0)
        host_starts = [r for r in starts if _host(r) == host]
        if len(errors) >= admission_streak and not host_starts:
            streak = errors[-admission_streak:]
            trips.append({"rule": "admission_error_streak", "host": host,
                          "since": streak[0].get("ts"),
                          "evidence": {"errors": streak, "job_starts": host_starts,
                                       "threshold": admission_streak}})

    assigned: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: _time(r) or 0):
        ts = _time(row)
        if ts is None or ts > end:
            continue
        if row.get("event") in {"job_assigned", "job_started", "in_progress"}:
            assigned[_host(row)] = row
        elif row.get("event") == "supervisor_signal" and start <= ts <= end:
            host = _host(row)
            prior = assigned.get(host)
            detail = str(row.get("detail") or "")
            if prior is not None and ("quarantine=" in detail or row.get("job_id") or row.get("run_id")):
                trips.append({"rule": "lane_death_by_signal", "host": host,
                              "since": row.get("ts"),
                              "evidence": {"signal": row, "assigned": prior}})
    return trips


def _gh_json(cli: str, path: str) -> Any:
    proc = subprocess.run([cli, "api", path], capture_output=True, text=True,
                          timeout=45, check=False)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"{cli} exited {proc.returncode}")
    return json.loads(proc.stdout)


def fetch_required_queue(cli: str, repo: str) -> list[dict[str, Any]]:
    """Read queued workflow runs once; required-class filtering is explicit."""
    payload = _gh_json(cli, f"repos/{repo}/actions/runs?status=queued&per_page=100")
    if not isinstance(payload, dict):
        raise RuntimeError("queued-runs response was not an object")
    runs = payload.get("workflow_runs", [])
    if not isinstance(runs, list):
        raise RuntimeError("queued-runs response had no workflow_runs list")
    return [r for r in runs if isinstance(r, dict) and r.get("required", True) is not False]


def trip_receipt(state: Path, trip: dict[str, Any], now: float | None = None) -> Path:
    state.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.fromtimestamp(time.time() if now is None else now,
                                      dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"serving-truth-{trip['rule']}-{trip['host']}-{stamp}"
    path = state / f"{stem}.json"
    suffix = 1
    while path.exists():
        path = state / f"{stem}-{suffix}.json"
        suffix += 1
    path.write_text(json.dumps({"kind": "tartci.serving-truth", "observed_at": stamp,
                                **trip}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def alert_pass(state: Path, events_path: Path, queued: Iterable[dict[str, Any]], *,
               now: float | None = None,
               issue: Callable[[str, str], tuple[int, str]] | None = None) -> dict[str, Any]:
    rows = classify(read_events(events_path), queued, now=now)
    receipts = [trip_receipt(state, trip, now=now) for trip in rows]
    # A single episode state makes repeated one-minute passes idempotent. The
    # receipt list remains append-only, so every trip is auditable.
    active = bool(rows)
    since = rows[0].get("since") if rows else None
    def render() -> tuple[str, str]:
        body = "\n\n".join(json.dumps(t, sort_keys=True, indent=2) for t in rows)
        return TITLE.format(rule=rows[0]["rule"]), "Serving-truth alarm tripped.\n\n" + body
    out = host_off.episode_alert(state / "serving-truth-alert.json", active=active,
                                 resolved=not active, since=since,
                                 raise_event=lambda: host_off.event(
                                     state, "serving_truth_alarm", "rules tripped",
                                     {"receipts": [str(p) for p in receipts]}, now),
                                 render=render, issue=issue, issues_enabled=True)
    return {"trips": rows, "receipts": [str(p) for p in receipts], **out}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--queue-json", type=Path)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--repo", default="danielraffel/tartci")
    parser.add_argument("--gh-cli", default=os.environ.get("TARTCI_GH_CLI", "ghapp"))
    args = parser.parse_args(argv)
    if args.queue_json:
        queued = json.loads(args.queue_json.read_text()).get("workflow_runs", [])
    else:
        queued = fetch_required_queue(args.gh_cli, args.repo)
    result = alert_pass(args.state, args.events, queued)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
