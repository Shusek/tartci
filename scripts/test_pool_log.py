#!/usr/bin/env python3
"""The pool-status log, driven in-process and through `tartci pool log`.

A fake runner stands in for `tartci pool status --json`: each test hands it
the statuses a host goes through and counts what it is asked to run.

Python 3.9-safe.
"""
from __future__ import annotations

import json
import os
import pathlib
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool_log  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
T0 = 1_791_900_000.0


def status(state: str = "on", participating: bool = True, **problems: Any) -> Dict[str, Any]:
    return {
        "host": "Daniels-Mac-Studio-m3",
        "state": state,
        "participating": participating,
        "fleet": {"config": {
            "tool_freshness": {"problem": problems.get("tool_freshness"), "age_hours": 24.7},
            "gate_reserve": {"problem": problems.get("gate_reserve")},
            "note": "not a section",
        }},
        "lanes": [{"label": "ignored", "pid": 123}],
    }


class Host:
    """A fake `pool status`: answers with the current status and counts calls."""

    def __init__(self) -> None:
        self.value: Any = status()
        self.calls: List[List[str]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> Tuple[int, str, str]:
        self.calls.append(list(argv))
        if isinstance(self.value, Exception):
            return 1, "", f"{self.value}\nmore detail"
        return 0, json.dumps(self.value), ""


class PoolLog(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.root)], check=False))
        self.dir = self.root / "state" / "pool-log"
        self.host = Host()

    def records(self) -> List[Dict[str, Any]]:
        log = self.dir / "events.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]

    def tick(self, minutes: float) -> Any:
        return pool_log.log_pass(T0 + minutes * 60, self.host, self.dir)

    def test_it_writes_on_a_change_and_an_hourly_heartbeat(self) -> None:
        self.assertIsNotNone(self.tick(0))
        for minutes in (5, 10, 55):
            self.assertIsNone(self.tick(minutes), minutes)
        heartbeat = self.tick(60)
        self.assertEqual(heartbeat["heartbeat"], True)
        self.host.value = status(tool_freshness="pulp 24.7 h behind")
        change = self.tick(65)
        self.assertNotIn("heartbeat", change)
        self.assertEqual(change["problems"], {"tool_freshness": "pulp 24.7 h behind"})
        self.assertIsNone(self.tick(70))
        self.host.value = status(state="off", participating=False,
                                 tool_freshness="pulp 24.7 h behind")
        self.assertEqual(self.tick(75)["state"], "off")
        self.assertEqual([record["at"] for record in self.records()],
                         [pool_log._iso(T0 + minutes * 60) for minutes in (0, 60, 65, 75)])
        self.assertEqual(len(self.host.calls), 8, "every pass reads the status")

    def test_a_record_holds_only_what_a_host_reader_classifies(self) -> None:
        self.host.value = status(gate_reserve="cores demand 6 against reserve 3\nsecond line",
                                 tool_freshness="x" * 900)
        record = self.tick(0)
        self.assertEqual(set(record), {"at", "host", "state", "participating", "problems"})
        self.assertEqual(record["problems"]["gate_reserve"],
                         "cores demand 6 against reserve 3\nsecond line")
        self.assertEqual(len(record["problems"]["tool_freshness"]), 500)
        self.assertNotIn("note", record["problems"])

    def test_a_read_failure_is_written_once_per_distinct_error(self) -> None:
        self.tick(0)
        self.host.value = RuntimeError("tartci: helper interpreter failed verification")
        first = self.tick(5)
        self.assertIn("pool status exited 1: tartci: helper interpreter failed verification",
                      first["error"])
        self.assertNotIn("more detail", first["error"])
        self.assertIsNone(self.tick(10))
        self.host.value = status()
        self.assertIsNotNone(self.tick(15), "recovery is a change")
        self.assertEqual(len(self.records()), 3)

    def test_the_log_rotates_and_keeps_two_files(self) -> None:
        os.environ["TARTCI_POOL_LOG_MAX_BYTES"] = "600"
        self.addCleanup(os.environ.pop, "TARTCI_POOL_LOG_MAX_BYTES", None)
        for minutes in range(0, 30 * 60, 60):
            self.host.value = status(tool_freshness=f"behind {minutes}")
            self.tick(minutes)
        names = sorted(path.name for path in self.dir.iterdir())
        self.assertEqual(names, ["events.jsonl", "events.jsonl.1", "last.json"])
        for name in ("events.jsonl", "events.jsonl.1"):
            self.assertLessEqual((self.dir / name).stat().st_size, 600, name)
        # The newest record survives the rotation.
        self.assertEqual(self.records()[-1]["problems"]["tool_freshness"], f"behind {29 * 60}")

    def test_it_writes_nothing_outside_its_state_directory(self) -> None:
        os.environ["TARTCI_HOME"] = str(self.root)
        self.addCleanup(os.environ.pop, "TARTCI_HOME", None)
        pool_log.log_pass(T0, self.host)
        written = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")
                         if path.is_file())
        self.assertEqual(written, ["state/pool-log/events.jsonl", "state/pool-log/last.json"])

    def test_the_status_reports_the_last_record_and_its_age(self) -> None:
        self.tick(0)
        report = pool_log.log_status(T0 + 125, self.dir)
        self.assertEqual(report["last"]["state"], "on")
        self.assertEqual(report["last_write_age_s"], 125)
        self.assertIn("events.jsonl", report["bytes"])
        empty = pool_log.log_status(T0, self.root / "nothing")
        self.assertIsNone(empty["last"])
        self.assertIsNone(empty["last_write_age_s"])

    def test_the_default_reads_this_generations_pool_status(self) -> None:
        os.environ.pop("TARTCI_POOL_LOG_STATUS_ARGV", None)
        self.assertEqual(pool_log.status_argv(),
                         ["/bin/bash", str(ROOT / "tartci"), "pool", "status", "--json"])

    def test_tartci_pool_log_runs_one_pass(self) -> None:
        fake = self.root / "fake-status.sh"
        fake.write_text("#!/bin/sh\necho '" + json.dumps(status(gate_reserve="low")) + "'\n")
        fake.chmod(0o755)
        environment = {**os.environ, "TARTCI_HOME": str(self.root),
                       "TARTCI_POOL_LOG_STATUS_ARGV": json.dumps([str(fake)])}
        for _ in range(2):
            done = subprocess.run(["/bin/bash", str(ROOT / "tartci"), "pool", "log", "--json"],
                                  capture_output=True, text=True, env=environment,
                                  stdin=subprocess.DEVNULL, timeout=60, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(self.records()), 1, "the second pass saw no change")
        self.assertEqual(self.records()[0]["problems"], {"gate_reserve": "low"})
        shown = subprocess.run(["/bin/bash", str(ROOT / "tartci"), "pool", "log", "--status",
                                "--json"], capture_output=True, text=True, env=environment,
                               stdin=subprocess.DEVNULL, timeout=60, check=False)
        self.assertEqual(json.loads(shown.stdout)["last"]["problems"], {"gate_reserve": "low"})

    def test_the_agent_template_runs_a_pass_every_five_minutes(self) -> None:
        source = (ROOT / "launchd" / "com.danielraffel.tartci.pool-log.plist.template").read_bytes()
        value = plistlib.loads(re.sub(rb"<!--.*?-->", b"", source, flags=re.DOTALL))
        self.assertEqual(value["ProgramArguments"][-2:], ["pool", "log"])
        self.assertEqual(value["StartInterval"], 300)
        self.assertTrue(value["RunAtLoad"])


if __name__ == "__main__":
    unittest.main()
