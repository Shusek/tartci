#!/usr/bin/env python3
"""VM lease waiters: a higher-priority VM lane is not beaten to the cores by a
lower one that merely called acquire first (scripts/leases.py, opt-in).

The shape is m5's on 2026-09-27: a 14-core lease universe whose 6-core non-gate
budget is held by a Shipyard-governed agent build (priority 40), leaving room
for exactly one 6-core gate VM, with several VM lanes each acquiring after a
~40 s admission precheck.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import leases

SCRIPT = Path(__file__).with_name("leases.py")

CAPACITY = 14
RESERVED = 8  # non-gate budget = 6: exactly the agent build's share
VM_KIND = "tart-macos-vm"


class WaiterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "leases"
        # Isolate from the host's installed fleet profile (its [leases] table
        # and agent floor must not leak into a unit test).
        self.profile = root / "fleet.toml"
        self.profile.write_text('schema = 1\n[host]\nid = "test"\n', encoding="utf-8")
        self.disk = root / "vms"
        self.disk.mkdir()
        self.env = {
            k: v for k, v in os.environ.items()
            if k not in ("TARTCI_RANK_VM_WAITERS", "TARTCI_VM_WAITER_FRESH_SECS")
        }
        self.env["TARTCI_FLEET_PROFILE"] = str(self.profile)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def knob(self, on: bool) -> None:
        self.profile.write_text(
            'schema = 1\n[host]\nid = "test"\n[leases]\n'
            f'rank_vm_waiters = {"true" if on else "false"}\n',
            encoding="utf-8",
        )

    def cli(self, *args: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *args, "--store-dir", str(self.store),
             "--capacity", str(CAPACITY), "--reserved-gate-cores", str(RESERVED),
             "--capacity-mem-mb", "0", "--agent-floor-cores", "0", "--json"],
            text=True, capture_output=True, check=False, env=self.env,
        )
        try:
            body = json.loads(proc.stdout)
        except ValueError:
            self.fail(f"{args}: rc={proc.returncode} stdout={proc.stdout} stderr={proc.stderr}")
        return proc.returncode, body

    def agent_build(self, lease_id: str = "agent-build", cores: int = 6) -> tuple[int, dict]:
        return self.cli("acquire", "--id", lease_id, "--cores", str(cores),
                        "--priority", "40", "--kind", "pulp-governed-build",
                        "--pid", str(os.getpid()))

    def vm(self, lease_id: str, priority: str, cores: int = 6, waiter: str = "",
           memory_only: bool = False) -> tuple[int, dict]:
        args = ["acquire", "--id", lease_id, "--priority", priority,
                "--kind", VM_KIND, "--pid", str(os.getpid()),
                "--disk-path", str(self.disk)]
        args += ["--cores", "0", "--memory-only", "--mem-mb", "4096"] if memory_only \
            else ["--cores", str(cores)]
        if waiter:
            args += ["--waiter-id", waiter]
        return self.cli(*args)

    def wait(self, waiter: str, priority: str, cores: int = 6, *, lane: str = "",
             pid: int | None = None, kind: str = VM_KIND) -> tuple[int, dict]:
        return self.cli("wait", "--id", waiter, "--cores", str(cores),
                        "--priority", priority, "--kind", kind,
                        "--lane", lane or waiter, "--pid", str(pid or os.getpid()))

    def waiter_rows(self) -> list[dict]:
        path = self.store / "waiters.json"
        return json.loads(path.read_text()) if path.exists() else []


class RaceTests(WaiterTestCase):
    def test_today_the_first_acquire_wins_whatever_its_priority(self) -> None:
        """Knob off (the default): the race observed on m5 reproduces."""
        self.assertEqual(self.agent_build()[0], 0)
        rc, body = self.wait("waiter-slot2", "120", lane="m5-pulp-gate-slot2")
        self.assertEqual(rc, 0)
        self.assertFalse(body["registered"])
        self.assertEqual(self.vm("vm-forge", "gate")[0], 0)  # forge clones first
        rc, body = self.vm("vm-slot2", "120", waiter="waiter-slot2")
        self.assertEqual(rc, 75)
        self.assertEqual(body["reason"], "capacity_exceeded")
        self.assertTrue(body["exceeded_axis"]["cores"])
        self.assertFalse((self.store / "waiters.json").exists())

    def test_with_the_knob_the_higher_priority_waiter_wins(self) -> None:
        self.knob(True)
        self.assertEqual(self.agent_build()[0], 0)
        rc, body = self.wait("waiter-slot2", "120", lane="m5-pulp-gate-slot2")
        self.assertEqual(rc, 0)
        self.assertTrue(body["registered"])
        rc, body = self.vm("vm-forge", "gate", waiter="waiter-forge")
        self.assertEqual(rc, 75)
        self.assertEqual(body["reason"], "deferred_to_waiter")
        self.assertEqual(body["waiter"]["lane"], "m5-pulp-gate-slot2")
        self.assertEqual(body["waiter"]["priority"], 120)
        self.assertEqual(body["exceeded_axis"], {"cores": False, "memory": False, "disk": False})
        rc, body = self.vm("vm-slot2", "120", waiter="waiter-slot2")
        self.assertEqual(rc, 0, body)
        self.assertEqual(self.waiter_rows(), [])  # the grant withdrew it
        rc, body = self.vm("vm-forge", "gate")
        self.assertEqual((rc, body["reason"]), (75, "capacity_exceeded"))

    def test_merge_group_waiter_outranks_a_pr_head_acquire(self) -> None:
        self.knob(True)
        self.agent_build()
        self.wait("waiter-slot1", "110")
        rc, body = self.vm("vm-slot2", "100")
        self.assertEqual((rc, body["reason"]), (75, "deferred_to_waiter"))
        self.assertEqual(body["waiter"]["priority"], 110)
        self.assertEqual(self.vm("vm-slot1", "110", waiter="waiter-slot1")[0], 0)

    def test_a_tie_stays_first_come(self) -> None:
        self.knob(True)
        self.agent_build()
        self.wait("waiter-slot2", "100")  # release PR gate leases as PR-head
        rc, body = self.vm("vm-forge", "gate")  # forge's gate class is also 100
        self.assertEqual(rc, 0, body)
        rc, body = self.vm("vm-slot2", "100", waiter="waiter-slot2")
        self.assertEqual((rc, body["reason"]), (75, "capacity_exceeded"))

    def test_a_denied_waiter_is_refreshed_and_keeps_its_place(self) -> None:
        self.knob(True)
        self.agent_build()
        self.assertEqual(self.vm("vm-big", "gate", cores=8)[0], 0)  # host now full
        self.wait("waiter-slot2", "120")
        rows = self.waiter_rows()
        old_seen = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=30)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows[0].update(seen_at=old_seen, waiting_since="2020-01-01T00:00:00Z")
        (self.store / "waiters.json").write_text(json.dumps(rows))
        rc, body = self.vm("vm-slot2", "120", waiter="waiter-slot2")
        self.assertEqual((rc, body["reason"]), (75, "capacity_exceeded"))
        row = self.waiter_rows()[0]
        self.assertNotEqual(row["seen_at"], old_seen)
        self.assertEqual(row["waiting_since"], "2020-01-01T00:00:00Z")
        self.wait("waiter-slot2", "120")  # an explicit refresh keeps it too
        self.assertEqual(self.waiter_rows()[0]["waiting_since"], "2020-01-01T00:00:00Z")


class ExpiryTests(WaiterTestCase):
    def test_a_stale_waiter_does_not_block(self) -> None:
        self.knob(True)
        self.agent_build()
        self.wait("waiter-slot2", "120")
        rows = self.waiter_rows()
        rows[0]["seen_at"] = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=91)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        (self.store / "waiters.json").write_text(json.dumps(rows))
        rc, body = self.vm("vm-forge", "gate")
        self.assertEqual(rc, 0, body)
        self.assertEqual(self.waiter_rows(), [])  # pruned under the lock

    def test_a_waiter_whose_owner_died_does_not_block(self) -> None:
        self.knob(True)
        self.agent_build()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            rc, _ = self.wait("waiter-slot2", "120", pid=child.pid)
            self.assertEqual(rc, 0)
            self.assertEqual(self.vm("vm-probe", "gate")[1]["reason"], "deferred_to_waiter")
        finally:
            child.kill()
            child.wait()
        rc, body = self.vm("vm-forge", "gate")
        self.assertEqual(rc, 0, body)

    def test_a_dead_owner_cannot_register(self) -> None:
        self.knob(True)
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        rc, body = self.wait("waiter-ghost", "120", pid=child.pid)
        self.assertEqual((rc, body["reason"]), (64, "waiter_owner_not_alive"))


class WorkConservingTests(WaiterTestCase):
    def test_a_higher_waiter_that_cannot_fit_does_not_block_one_that_does(self) -> None:
        self.knob(True)
        self.agent_build()
        self.wait("waiter-release", "120", cores=12)  # 8 free: it cannot boot anyway
        rc, body = self.vm("vm-forge", "gate")
        self.assertEqual(rc, 0, body)

    def test_a_grant_that_leaves_room_for_the_waiter_is_not_deferred(self) -> None:
        self.knob(True)
        # No agent build: 14 free, two 6-core VMs fit together.
        self.wait("waiter-slot2", "120")
        rc, body = self.vm("vm-forge", "gate")
        self.assertEqual(rc, 0, body)
        self.assertEqual(self.vm("vm-slot2", "120", waiter="waiter-slot2")[0], 0)

    def test_a_parked_vm_upgrade_is_ranked_like_an_acquire(self) -> None:
        self.knob(True)
        self.agent_build()
        self.assertEqual(self.vm("vm-warm", "gate", memory_only=True)[0], 0)
        self.wait("waiter-slot2", "120")
        rc, body = self.cli("resize", "--id", "vm-warm", "--cores", "6", "--mem-mb", "4096",
                            "--priority", "gate")
        self.assertEqual((rc, body["reason"]), (75, "deferred_to_waiter"))
        status = self.cli("status")[1]
        warm = next(r for r in status["leases"] if r["id"] == "vm-warm")
        self.assertEqual(warm["lease_size_cores"], 0)  # denial left it parked


class AgentBuildTests(WaiterTestCase):
    def test_the_agent_builds_non_gate_share_is_unchanged(self) -> None:
        outcomes = {}
        for on in (False, True):
            with self.subTest(knob=on):
                self.tearDown()
                self.setUp()
                self.knob(on)
                # A top-priority VM waiter that would fit only in cores the
                # agent build is about to take, if the budgets were shared.
                self.wait("waiter-slot2", "120", cores=14)
                self.wait("waiter-slot1", "110", cores=6)
                rc, body = self.agent_build(cores=6)
                self.assertEqual(rc, 0, body)
                rc2, body2 = self.agent_build("agent-build-2", cores=1)
                outcomes[on] = (rc, body["capacity"], rc2, body2.get("reason"))
        self.assertEqual(outcomes[False], outcomes[True])
        self.assertEqual(outcomes[True][1]["non_gate_used_cores"], 6)
        self.assertEqual(outcomes[True][2:], (75, "capacity_exceeded"))

    def test_a_build_cannot_register_as_a_waiter(self) -> None:
        self.knob(True)
        rc, body = self.wait("waiter-agent", "40", kind="pulp-governed-build")
        self.assertEqual((rc, body["reason"]), (64, "waiter_requires_vm_kind"))


class KnobOffTests(WaiterTestCase):
    def test_knob_off_ignores_and_never_touches_the_waiter_file(self) -> None:
        self.knob(True)
        self.agent_build()
        self.wait("waiter-slot2", "120")
        before = (self.store / "waiters.json").read_bytes()
        self.knob(False)
        rc, body = self.vm("vm-forge", "gate", waiter="waiter-forge")
        self.assertEqual(rc, 0, body)
        rc, body = self.vm("vm-slot2", "120", waiter="waiter-slot2")
        self.assertEqual((rc, body["reason"]), (75, "capacity_exceeded"))
        self.assertEqual((self.store / "waiters.json").read_bytes(), before)
        self.assertNotIn("waiters", self.cli("status")[1])

    def test_knob_off_admission_is_byte_for_byte_todays(self) -> None:
        def run(with_waiter_file: bool) -> list:
            self.tearDown()
            self.setUp()
            if with_waiter_file:
                self.store.mkdir(parents=True)
                (self.store / "waiters.json").write_text(json.dumps([{
                    "id": "w", "command_kind": VM_KIND, "priority": 999,
                    "lease_size_cores": 6, "lease_size_mem_mb": 0,
                    "pid": os.getpid(), "process_start_time": leases.pid_start(os.getpid()),
                    "host_boot_time": leases.host_boot_time(),
                    "seen_at": "2999-01-01T00:00:00Z"}]))
            seq = [self.agent_build(), self.vm("vm-forge", "gate", waiter="x"),
                   self.vm("vm-slot2", "120", waiter="w")]
            return [(rc, body.get("reason"), body.get("capacity")) for rc, body in seq]
        self.assertEqual(run(False), run(True))


class ProfileKnobTests(unittest.TestCase):
    def test_leases_table_and_environment(self) -> None:
        import host_profile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "p.toml"
            env = {k: v for k, v in os.environ.items()
                   if k not in ("TARTCI_RANK_VM_WAITERS", "TARTCI_VM_WAITER_FRESH_SECS")}
            saved = dict(os.environ)
            try:
                os.environ.clear()
                os.environ.update(env)
                path.write_text("schema = 1\n")
                self.assertFalse(host_profile.lease_policy_settings(str(path))["rank_vm_waiters"])
                path.write_text("schema = 1\n[leases]\nrank_vm_waiters = true\n"
                                "waiter_fresh_secs = 120\n")
                got = host_profile.lease_policy_settings(str(path))
                self.assertTrue(got["rank_vm_waiters"])
                self.assertEqual(got["vm_waiter_fresh_secs"], 120)
                os.environ["TARTCI_RANK_VM_WAITERS"] = "0"
                self.assertFalse(host_profile.lease_policy_settings(str(path))["rank_vm_waiters"])
            finally:
                os.environ.clear()
                os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
