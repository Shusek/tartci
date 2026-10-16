"""Regression tests for the Tart boot/admission wedge watchdog."""

from __future__ import annotations

import json
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tartci_launchd_watchdog as wd  # noqa: E402


class StaleBootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.plist = self.root / "lane.plist"
        self.plist.write_bytes(plistlib.dumps({
            "Label": "com.danielraffel.tartci.tart-runner-macos-fleet.m1.lane",
            "EnvironmentVariables": {
                "TARTCI_STATE_DIR": str(self.state),
                "TARTCI_RUNNER_NAME": "lane",
            },
        }))

    def write_state(self, **updates: object) -> None:
        state = {"ts": 1_000.0, "phase": "booting", "supervisor_pid": "42"}
        state.update(updates)
        (self.state / "lane.state.json").write_text(json.dumps(state))

    def test_stale_boot_requires_matching_launchd_pid(self) -> None:
        self.write_state()
        probe = wd.probe_stale_boot(str(self.plist), 42, now=2_000.0)
        self.assertTrue(probe.stale, probe.reason)
        mismatch = wd.probe_stale_boot(str(self.plist), 43, now=2_000.0)
        self.assertFalse(mismatch.stale, mismatch.reason)

    def test_runner_iso_heartbeat_timestamp_is_supported(self) -> None:
        self.write_state(ts="1970-01-01T00:16:40Z")
        probe = wd.probe_stale_boot(str(self.plist), 42, now=2_000.0)
        self.assertTrue(probe.stale, probe.reason)

    def test_rendered_fleet_prefix_and_slot_identity_is_supported(self) -> None:
        rendered = self.root / "rendered.plist"
        rendered.write_bytes(plistlib.dumps({
            "Label": "com.danielraffel.tartci.tart-runner-macos-fleet.m1.lane",
            "EnvironmentVariables": {
                "TARTCI_STATE_DIR": str(self.state),
                "TARTCI_RUNNER_NAME_PREFIX": "m1-lane",
                "TARTCI_RUNNER_SLOT": "1",
            },
        }))
        (self.state / "m1-lane-01.state.json").write_text(json.dumps({
            "ts": "1970-01-01T00:16:40Z", "phase": "booting",
            "supervisor_pid": "42",
        }))
        probe = wd.probe_stale_boot(str(rendered), 42, now=2_000.0)
        self.assertTrue(probe.stale, probe.reason)

    def test_active_job_and_fresh_heartbeat_are_safe(self) -> None:
        self.write_state(run_id="run-1")
        active = wd.probe_stale_boot(str(self.plist), 42, now=2_000.0)
        self.assertFalse(active.stale, active.reason)
        self.write_state(run_id="")
        fresh = wd.probe_stale_boot(str(self.plist), 42, now=1_100.0)
        self.assertFalse(fresh.stale, fresh.reason)

    def test_corrupt_state_is_unknown_and_missing_state_is_not_a_wedge(self) -> None:
        (self.state / "lane.state.json").write_text("not json")
        corrupt = wd.probe_stale_boot(str(self.plist), 42, now=2_000.0)
        self.assertIsNone(corrupt.stale, corrupt.reason)
        (self.state / "lane.state.json").unlink()
        missing = wd.probe_stale_boot(str(self.plist), 42, now=2_000.0)
        self.assertFalse(missing.stale, missing.reason)


if __name__ == "__main__":
    unittest.main()
