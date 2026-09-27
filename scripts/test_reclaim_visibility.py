#!/usr/bin/env python3
"""A dead or shadowed reclaim agent is visible, not silent.

Both faults are exercised with their healthy control in the same test, because
a check that always reports "fine" passes every test that only feeds it a
healthy host.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fleet_doctor as fd  # noqa: E402
import reclaim_status as rs  # noqa: E402

HOME = pathlib.Path("/Users/someone")


def launchctl(jobs: dict[str, str | None], list_rc: int = 0):
    """A launchctl double: `jobs` maps label -> plist path launchd holds."""
    def run(argv: list[str]) -> tuple[int, str, str]:
        if argv[1:] == ["list"]:
            if list_rc:
                return list_rc, "", "boom"
            rows = ["PID\tStatus\tLabel", "-\t0\tcom.apple.Finder"]
            rows += [f"-\t0\t{label}" for label in jobs]
            return 0, "\n".join(rows) + "\n", ""
        label = argv[2].rsplit("/", 1)[1]
        path = jobs.get(label)
        body = f"{label} = {{\n\tactive count = 0\n" + (f"\tpath = {path}\n" if path else "") + "}\n"
        return 0, body, ""
    return run


class LeakedRegistration(unittest.TestCase):
    real = str(HOME / "Library/LaunchAgents/com.danielraffel.tartci.reclaim.plist")

    def test_a_label_loaded_from_a_temp_home_is_a_problem_and_named(self):
        leaked = "/private/var/folders/x/T/tmpabc/home/Library/LaunchAgents/com.danielraffel.tartci.reclaim.plist"
        rows, _ = fd.launchd_registrations(launchctl({
            "com.danielraffel.tartci.reclaim": leaked,
            "com.danielraffel.tartci.reap": str(HOME / "Library/LaunchAgents/com.danielraffel.tartci.reap.plist"),
        }))
        finding = fd.check_launchd_registrations(rows, home=HOME)
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "launchd_registration_leaked"))
        self.assertIn(leaked, finding.detail)
        self.assertEqual([r["label"] for r in finding.facts["leaked"]],
                         ["com.danielraffel.tartci.reclaim"])

    def test_the_same_labels_from_the_real_home_are_ok(self):
        rows, _ = fd.launchd_registrations(launchctl({
            "com.danielraffel.tartci.reclaim": self.real,
            "com.danielraffel.pulp.tart-runner": str(HOME / "Library/LaunchAgents/com.danielraffel.pulp.tart-runner.plist"),
        }))
        finding = fd.check_launchd_registrations(rows, home=HOME)
        self.assertEqual((finding.state, finding.facts["loaded"]), (fd.OK, 2))

    def test_a_nested_or_pathless_registration_is_leaked_too(self):
        rows, _ = fd.launchd_registrations(launchctl({
            "com.danielraffel.tartci.reclaim": str(HOME / "Library/LaunchAgents/sub/x.plist"),
            "com.danielraffel.tartci.reap": None,
        }))
        finding = fd.check_launchd_registrations(rows, home=HOME)
        self.assertEqual(len(finding.facts["leaked"]), 2)

    def test_non_tartci_labels_are_not_judged(self):
        rows, _ = fd.launchd_registrations(launchctl({"homebrew.mxcl.postgresql": "/tmp/x.plist"}))
        self.assertEqual(rows, [])

    def test_unreadable_launchd_is_unknown_not_clean(self):
        rows, error = fd.launchd_registrations(launchctl({}, list_rc=1))
        finding = fd.check_launchd_registrations(rows, home=HOME, error=error)
        self.assertEqual(finding.state, fd.UNKNOWN)

    def test_every_new_code_has_a_reason_row(self):
        reasons = json.loads((HERE / "fleet_reasons.json").read_text())["reasons"]
        for code in ("launchd_registration_leaked", "launchd_registrations_ok",
                     "launchd_registrations_unreadable", "reclaim_ok", "reclaim_failed",
                     "reclaim_stale", "reclaim_never_recorded", "reclaim_unreadable"):
            self.assertIn(code, fd.CODES)
            self.assertIn(code, reasons)


class LastPass(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, **fields):
        receipt = {"finished_ts": time.time(), "exit_code": 0, "reclaimed_bytes": 5 * rs.GIB,
                   "pulp_reapers": {"enabled": True, "reclaimed_bytes": 4 * rs.GIB}}
        receipt.update(fields)
        (self.dir / "last-run.json").write_text(json.dumps(receipt))

    def test_recent_ok_pass_is_ok(self):
        self.write()
        value = rs.status(self.dir, log_path=self.dir / "none.log")
        self.assertEqual(value["state"], "ok")
        self.assertEqual(fd.check_reclaim(value).state, fd.OK)
        self.assertIn("pulp reapers 4.0 GiB", rs.describe(value))

    def test_a_receipt_older_than_two_missed_passes_is_stale_and_a_problem(self):
        # The m3 shape: the agent exited 127 hourly for a day and wrote nothing.
        self.write(finished_ts=time.time() - 26 * 3600)
        value = rs.status(self.dir, log_path=self.dir / "none.log")
        self.assertEqual(value["state"], "stale")
        finding = fd.check_reclaim(value)
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "reclaim_stale"))
        self.assertIn("STALE", finding.detail)

    def test_stale_wins_over_an_old_success(self):
        self.write(finished_ts=time.time() - rs.STALE_AFTER_S - 60, exit_code=0)
        self.assertEqual(rs.status(self.dir)["state"], "stale")

    def test_recent_failed_pass_is_a_problem(self):
        self.write(exit_code=3)
        finding = fd.check_reclaim(rs.status(self.dir))
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "reclaim_failed"))

    def test_no_receipt_is_unknown_and_garbage_is_unreadable(self):
        self.assertEqual(fd.check_reclaim(rs.status(self.dir)).code, "reclaim_never_recorded")
        (self.dir / "last-run.json").write_text("{nope")
        self.assertEqual(fd.check_reclaim(rs.status(self.dir)).code, "reclaim_unreadable")

    def test_pool_status_carries_the_reclaim_state(self):
        # `tartci pool status` must print it in both forms; a grep is the whole
        # assertion because the shell composes the JSON by hand.
        body = (HERE.parent / "tartci").read_text()
        start = body.index("cmd_pool()")
        section = body[start:body.index("\n}\n", start)]
        self.assertIn('"reclaim":%s', section)
        self.assertIn('scripts/reclaim_status.py" --json', section)
        self.assertIn('scripts/reclaim_status.py" 2>/dev/null', section)

    def test_cli_reads_the_receipt(self):
        self.write(exit_code=4)
        out = subprocess.run([sys.executable, str(HERE / "reclaim_status.py"), "--json",
                              "--state-dir", str(self.dir)], capture_output=True, text=True,
                             check=True, env={**os.environ})
        self.assertEqual(json.loads(out.stdout)["state"], "failed")


if __name__ == "__main__":
    unittest.main()
