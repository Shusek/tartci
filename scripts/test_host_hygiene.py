#!/usr/bin/env python3
"""Disk floor and foreign-VM reports (scripts/host_hygiene.py)."""
from __future__ import annotations

import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import host_hygiene as hy  # noqa: E402
import tartci_incident  # noqa: E402
import vm_boot_alert  # noqa: E402

NOW = 1792120000.0
GB = hy.GB


def vol(path: str, free_gb: float, total_gb: float) -> dict:
    return {"paths": [path], "free": free_gb * GB, "total": total_gb * GB,
            "free_pct": 100.0 * free_gb / total_gb}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.user = self.root / "user"
        self.store = self.root / "VMs"
        (self.store / "vms").mkdir(parents=True)
        (self.user / ".tart" / "vms").mkdir(parents=True)
        (self.user / ".config" / "tartci").mkdir(parents=True)
        (self.user / "Library" / "LaunchAgents").mkdir(parents=True)
        self.enable = self.user / ".config" / "tartci" / "host-hygiene.enabled"
        self.enable.write_text("")
        env = {"HOME": str(self.user), "TARTCI_HOME": str(self.user / ".tartci"),
               "TART_HOME": str(self.store),
               "TARTCI_MACOS_FLEET_PROFILE": str(self.user / "absent.toml")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("TARTCI_LAUNCH_AGENTS_DIR", "TARTCI_HOST_HYGIENE_ENABLE_FILE"):
            os.environ.pop(key, None)
        host = mock.patch.object(vm_boot_alert, "_alert_host", return_value=("m1", "m1"))
        host.start()
        self.addCleanup(host.stop)
        self.opened: list = []
        self.closed: list = []
        self.measured: list = []
        self.listings: dict = {self.store: [], self.user / ".tart": []}

    def vm(self, store: Path, name: str, state: str = "stopped", age_days: float = 0,
           size: int = 20) -> None:
        self.listings.setdefault(store, []).append(
            {"Name": name, "State": state, "Size": size, "Source": "local"})
        bundle = store / "vms" / name
        bundle.mkdir(parents=True, exist_ok=True)
        os.utime(bundle, (NOW - age_days * 86400, NOW - age_days * 86400))

    def run_pass(self, now: float = NOW, free_gb: float = 900, ps: str = "") -> list[str]:
        def measure(path: Path):
            self.measured.append(path)
            return {"agent-scratch": 432, "agent-worktrees": 138}.get(path.name, 1) * GB

        return hy.alert_pass(
            now, user_home=self.user,
            vols=lambda paths: [vol(str(paths[0]), free_gb, 2000)],
            measure=measure, listing=lambda store: self.listings.get(store, []),
            ps=lambda: ps,
            issue=lambda t, b: self.opened.append((t, b)) or (0, str(70 + len(self.opened))),
            close=lambda n: self.closed.append(n) or (0, "closed"))


class DiskFloor(Base):
    def test_below_twenty_percent_opens_one_issue_with_consumers_and_reclaim(self) -> None:
        lines = self.run_pass(free_gb=150)
        self.assertTrue(any("WARN host-disk" in line and "150 GB free (8%)" in line
                            for line in lines), lines)
        self.run_pass(NOW + 300, free_gb=150)
        disk = [(t, b) for t, b in self.opened if "disk floor" in t]
        self.assertEqual(len(disk), 1)
        title, body = disk[0]
        self.assertIn("[tartci] m1 is below its disk floor: 150 GB free (8%)", title)
        self.assertLess(body.index("agent-scratch`: 432 GB"), body.index("agent-worktrees`: 138 GB"))
        self.assertIn("never rm -rf", body)
        self.assertIn("tartci reclaim", body)
        self.assertEqual(tartci_incident.parse(body)["class"], "host_disk_floor")
        # The sizes are measured once and reused inside their TTL.
        self.assertEqual(len([p for p in self.measured if p.name == "agent-scratch"]), 1)

    def test_under_two_hundred_gb_breaches_even_above_twenty_percent(self) -> None:
        self.assertEqual(len(hy.breaches([vol("/x", 190, 500)])), 1)
        self.assertEqual(hy.breaches([vol("/x", 400, 1000)]), [])   # control: 40%, 400 GB
        self.assertEqual(len(hy.breaches([vol("/x", 300, 2000)])), 1)  # 300 GB but 15%

    def test_recovery_closes_the_issue_and_nothing_is_deleted(self) -> None:
        scratch = self.user / "agent-scratch" / "old-run"
        scratch.mkdir(parents=True)
        self.run_pass(free_gb=150)
        self.assertEqual(self.run_pass(NOW + 300, free_gb=900), [])
        self.assertEqual(self.closed, ["71"])
        self.assertTrue(scratch.exists())

    def test_volumes_are_counted_once_per_device(self) -> None:
        stat = mock.Mock(return_value=mock.Mock(f_blocks=100, f_frsize=GB, f_bavail=10))
        rows = hy.volumes([Path("/a"), Path("/b")], stat=stat, device=lambda p: 7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["paths"], ["/a", "/b"])


class ForeignVms(Base):
    def test_old_stopped_and_long_running_foreign_vms_are_reported_not_touched(self) -> None:
        self.vm(self.user / ".tart", "vellum-pr19-linux-20260811", age_days=60, size=6)
        self.vm(self.store, "lv2repro-pulpimpl", state="running", size=20)
        self.vm(self.store, "fresh-manual", age_days=3)                 # under 14 d
        self.vm(self.store, "m1-pulp-gate-01-23177-1", state="running")  # a lane clone
        ps = ("11:02:03 /opt/homebrew/bin/tart run --no-graphics lv2repro-pulpimpl\n"
              "12:00:00 /opt/homebrew/bin/tart run --no-graphics m1-pulp-gate-01-23177-1\n")   # a long job, still tartci's
        lines = self.run_pass(ps=ps)
        line = next(x for x in lines if "foreign-vms" in x)
        self.assertIn("lv2repro-pulpimpl running 11.0 h", line)
        self.assertIn("vellum-pr19-linux-20260811 stopped 60 d", line)
        self.assertNotIn("fresh-manual", line)
        self.assertNotIn("m1-pulp-gate", line)
        (title, body), = [(t, b) for t, b in self.opened if "did not make" in t]
        self.assertEqual(title, "[tartci] m1 has 2 VM(s) tartci did not make")
        self.assertIn("tart stop lv2repro-pulpimpl", body)
        self.assertEqual(tartci_incident.parse(body)["class"], "host_foreign_vms")
        self.assertTrue((self.user / ".tart" / "vms" / "vellum-pr19-linux-20260811").exists())

    def test_a_short_foreign_run_is_not_reported(self) -> None:
        self.vm(self.store, "lv2repro-pulpimpl", state="running")
        ps = "03:59:00 /opt/homebrew/bin/tart run lv2repro-pulpimpl\n"
        self.assertEqual([x for x in self.run_pass(ps=ps) if "foreign" in x], [])

    def test_goldens_and_names_a_tartci_agent_carries_are_tartci_s(self) -> None:
        self.vm(self.store, "pulp-linux-build:latest", age_days=60)
        self.vm(self.store, "pulp-build-runner:latest", age_days=60)
        plist = self.user / "Library" / "LaunchAgents" / "com.danielraffel.pulp.tart-runner-linux.plist"
        plist.write_bytes(plistlib.dumps({"EnvironmentVariables": {
            "TARTCI_GOLDEN": "pulp-linux-build:latest"}}))
        profile = {"lane": [{"golden": "pulp-build-runner:latest"}]}
        with mock.patch.object(hy, "_profile", return_value=profile):
            lines = self.run_pass()
        self.assertEqual([x for x in lines if "foreign" in x], [])
        # Control: without the agent naming it, the linux golden is reported.
        plist.unlink()
        os.unlink(self.user / ".tartci" / "state" / "host-hygiene" / "foreign-vms.json")
        with mock.patch.object(hy, "_profile", return_value=profile):
            lines = self.run_pass()
        self.assertIn("pulp-linux-build:latest", "".join(lines))

    def test_it_scans_hourly_and_reports_a_store_it_cannot_list(self) -> None:
        self.listings[self.store] = None
        lines = self.run_pass()
        self.assertIn(f"could not list {self.store}", "".join(lines))
        self.listings[self.store] = []
        self.assertIn("could not list", "".join(self.run_pass(NOW + 600)))   # not rescanned
        self.assertNotIn("could not list", "".join(self.run_pass(NOW + 3600)))


class OptIn(Base):
    def test_off_without_the_enable_file(self) -> None:
        self.enable.unlink()
        self.vm(self.store, "old-thing", age_days=60)
        self.assertEqual(self.run_pass(free_gb=10), [])
        self.assertEqual((self.opened, self.measured), ([], []))


class Wiring(unittest.TestCase):
    def test_the_heal_pass_runs_it(self) -> None:
        source = (Path(hy.__file__).parent / "tartci_launchd_watchdog.py").read_text()
        self.assertIn("hygiene_line = host_hygiene_pass()", source)


if __name__ == "__main__":
    unittest.main()
