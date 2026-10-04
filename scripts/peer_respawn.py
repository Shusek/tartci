#!/usr/bin/env python3
"""Start a sibling fleet lane that exited 75 and that launchd never respawned.

A lane supervisor exits 75 (EX_TEMPFAIL) only after its fail-closed restart
contract has run, and relies on launchd KeepAlive to start it again. On a host
whose launchd defers spawns, that respawn can wait indefinitely: on m3 on
2026-10-04 studio-pulp-gate-01 sat stopped for more than an hour after its
exit 75 while `launchctl print` showed `pended nondemand spawn = inefficient`.
Interval agents (the launchd watchdog among them) starve the same way, so the
watchdog cannot be the only remedy. Lane supervisors are long-running and keep
working, so each one checks its siblings once per pass.

The rule is the watchdog's own (`owes_exit75_respawn`): pool participation on,
the service not disabled, last exit 75, not running, and its state file older
than the grace. The remedy is a plain `launchctl kickstart` (never `-k`), the
start launchd owed. Every kick is verified on the next pass, rate limited, and
capped per hour, so a lane that keeps exiting 75 fails loud rather than being
restarted forever. A stopped sibling with any other exit code is reported and
left to the watchdog's verdict: only 75 vouches that the lane's restart
contract ran.

Prints one `EVENT<TAB>name<TAB>detail` line per event for the caller to log.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_lane_discovery  # noqa: E402
import tartci_launchd_watchdog as watchdog  # noqa: E402

DEFAULT_GRACE_S = 120
DEFAULT_MIN_INTERVAL_S = 300
DEFAULT_MAX_PER_HOUR = 3
HOUR_S = 3600
STOPPED_STATES = {"not running", "spawn scheduled"}

Run = Callable[[list[str]], "tuple[int, str, str]"]


def _phase(state_file: Path) -> str | None:
    try:
        return json.loads(state_file.read_text(encoding="utf-8")).get("phase")
    except (OSError, ValueError, AttributeError):
        return None


def _state_file(lane: fleet_lane_discovery.Lane) -> Path | None:
    if lane.state_dir is None:
        return None
    return lane.state_dir / f"{lane.runner_name}.state.json"


def run_pass(
    *,
    self_label: str,
    lanes: list[fleet_lane_discovery.Lane],
    participating: bool,
    disabled: set[str] | None,
    ledger: dict,
    run: Run,
    now: float,
    domain: str,
    grace_s: int = DEFAULT_GRACE_S,
    min_interval_s: int = DEFAULT_MIN_INTERVAL_S,
    max_per_hour: int = DEFAULT_MAX_PER_HOUR,
) -> list[tuple[str, str]]:
    """One pass over the siblings. Mutates `ledger`; returns (event, detail) pairs."""
    events: list[tuple[str, str]] = []
    for lane in lanes:
        if lane.label == self_label:
            continue
        record = ledger.setdefault(lane.label, {"kicks": []})
        record["kicks"] = [t for t in record.get("kicks", []) if now - t < HOUR_S]
        state_file = _state_file(lane)
        phase = _phase(state_file) if state_file else None
        rc, out, _ = run(["launchctl", "print", f"{domain}/{lane.label}"])
        state, last_exit = watchdog.parse_launchctl_print(out) if rc == 0 else (None, None)

        pending = record.pop("confirm_after", None)
        if pending is not None:
            started = state == "running" or (phase is not None and phase != "stopped")
            events.append((
                "peer_respawn_confirmed" if started else "peer_respawn_unconfirmed",
                f"label={lane.label} state={state} phase={phase}",
            ))

        if state not in STOPPED_STATES:
            record.pop("reported_exit", None)
            continue
        if last_exit not in (None, 0, 75):
            if record.get("reported_exit") != last_exit:
                record["reported_exit"] = last_exit
                events.append(("peer_sibling_stopped",
                               f"label={lane.label} last_exit={last_exit} phase={phase}"))
            continue
        if disabled is None:
            continue  # enablement unknown: fail closed, as the watchdog does
        expected_loaded = participating and lane.label not in disabled
        age = None
        if state_file is not None:
            try:
                age = max(0.0, now - state_file.stat().st_mtime)
            except OSError:
                age = None
        if not watchdog.owes_exit75_respawn(state, last_exit, age, expected_loaded, grace_s):
            continue
        if len(record["kicks"]) >= max_per_hour:
            if not record.get("ceiling_reported"):
                record["ceiling_reported"] = True
                events.append(("peer_respawn_ceiling",
                               f"label={lane.label} kicks_last_hour={len(record['kicks'])}"))
            continue
        record.pop("ceiling_reported", None)
        if record["kicks"] and now - record["kicks"][-1] < min_interval_s:
            continue
        pended = "yes" if "pended nondemand spawn" in out else "no"
        kick_rc, _, kick_err = run(["launchctl", "kickstart", f"{domain}/{lane.label}"])
        record["kicks"].append(now)
        record["confirm_after"] = now
        events.append(("peer_respawn",
                       f"label={lane.label} last_exit=75 stopped_for={int(age)}s "
                       f"pended={pended} kick_rc={kick_rc}"
                       + (f" err={kick_err.strip()[:120]}" if kick_rc else "")))
    return events


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-label", required=True)
    parser.add_argument("--ledger", type=Path,
                        default=Path(os.environ.get("TARTCI_HOME", Path.home() / ".tartci"))
                        / "state" / "peer-respawn.json")
    parser.add_argument("--participation-file",
                        default=os.path.join(os.path.expanduser("~"), ".config", "tartci",
                                             "native-build-participation"))
    parser.add_argument("--grace-seconds", type=int, default=DEFAULT_GRACE_S)
    args = parser.parse_args(argv)

    lanes, _problems = fleet_lane_discovery.discover_lanes()
    if lanes is None:
        return 0  # launchd unreadable: nothing is known, so nothing is kicked
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{args.ledger}.lock", "a") as lock:
        # One sibling at a time across the host's supervisors, so two lanes
        # never kick the same sibling in the same instant.
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = _load(args.ledger)
        events = run_pass(
            self_label=args.self_label,
            lanes=lanes,
            participating=watchdog.pool_participating(args.participation_file),
            disabled=watchdog.disabled_services(),
            ledger=ledger,
            run=watchdog._run,
            now=time.time(),
            domain=watchdog._domain(),
            grace_s=args.grace_seconds,
        )
        tmp = args.ledger.with_suffix(".tmp")
        tmp.write_text(json.dumps(ledger), encoding="utf-8")
        os.replace(tmp, args.ledger)
    for name, detail in events:
        print(f"EVENT\t{name}\t{detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
