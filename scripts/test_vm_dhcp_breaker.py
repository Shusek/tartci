#!/usr/bin/env python3
"""A host whose VM DHCP stopped answering stops cloning, probes, and recovers.

Pins: two `no_ip` within 15 min with no address between open the breaker, and
nothing less does; while open no lane clones except one probe per cadence (or
at once when bootpd's run counter moves), and two lanes in one cadence never
both probe; the first address closes it, as does a reboot after it opened; an
unreadable breaker reads closed; writes are atomic; the doctor names the root
remedy that tartci never runs; and run_one checks the breaker before the job
claim.

Run:  python3 scripts/test_vm_dhcp_breaker.py
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_doctor  # noqa: E402
import vm_dhcp_breaker as vb  # noqa: E402

BREAKER = ROOT / "scripts" / "vm_dhcp_breaker.py"
LIB = ROOT / "providers" / "tart-macos" / "vm-dhcp.lib.sh"
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
T0 = 2_000_000_000.0


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runs = self.tmp / "bootpd-runs"
        self.runs.write_text("3")
        launchctl = self.tmp / "launchctl"
        launchctl.write_text(
            "#!/bin/bash\n"
            "printf '\\tstate = not running\\n\\truns = %s\\n\\tlast exit code = 0\\n' "
            f"\"$(cat {str(self.runs)!r})\"\n")
        launchctl.chmod(0o755)
        self.env = {"TARTCI_VM_DHCP_DIR": str(self.tmp / "vm-dhcp"),
                    "TARTCI_VM_DHCP_LAUNCHCTL": str(launchctl),
                    "TARTCI_VM_DHCP_BOOT_TIME": str(T0 - 86400)}
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def record(self, outcome: str, at: float, lane: str = "m5-pulp-gate", vm: str = "vm") -> dict:
        return vb.record(argparse.Namespace(outcome=outcome, lane=lane, vm=vm), now=at)

    def check(self, at: float, lane: str = "m5-pulp-gate") -> dict:
        return vb.check(argparse.Namespace(lane=lane), now=at)

    def names(self, result: dict) -> list[str]:
        return [name for name, _ in result["events"]]

    def state(self) -> dict:
        return vb.status(self.tmp / "vm-dhcp")


class Trigger(Case):
    def test_two_no_ip_within_the_window_open_it_with_the_evidence(self):
        self.assertEqual(self.names(self.record("no_ip", T0)), [])
        result = self.record("no_ip", T0 + 300, lane="m5-pulp-gate-slot2")
        self.assertEqual(self.names(result), ["vm_dhcp_unanswered"])
        detail = result["events"][0][1]
        for token in ("streak=2", "window_s=300", "lanes=m5-pulp-gate,m5-pulp-gate-slot2",
                      "bootpd_state=not running", "bootpd_runs=3", "bootpd_last_exit=0",
                      "last_no_ip=2033-05-18T03:33:20Z,2033-05-18T03:38:20Z"):
            self.assertIn(token, detail)
        self.assertEqual(self.state()["state"], "open")

    def test_two_no_ip_further_apart_do_not(self):
        self.record("no_ip", T0)
        self.assertEqual(self.names(self.record("no_ip", T0 + 20 * 60)), [])
        self.assertEqual(self.state()["state"], "closed")

    def test_an_address_between_them_resets_the_streak(self):
        self.record("no_ip", T0)
        self.record("ip", T0 + 60)
        self.assertEqual(self.names(self.record("no_ip", T0 + 120)), [])
        self.assertEqual(self.state()["state"], "closed")


class Open(Case):
    def open(self) -> None:
        self.record("no_ip", T0)
        self.record("no_ip", T0 + 60)

    def test_lanes_back_off_until_the_probe_cadence(self):
        self.open()
        self.assertEqual(self.check(T0 + 61), {"action": "backoff", "events": []})
        result = self.check(T0 + 60 + 300)
        self.assertEqual(result["action"], "probe")
        self.assertIn("trigger=cadence", result["events"][0][1])

    def test_one_probe_per_cadence_across_lanes(self):
        self.open()
        self.assertEqual(self.check(T0 + 400, lane="a")["action"], "probe")
        self.assertEqual(self.check(T0 + 401, lane="b"), {"action": "backoff", "events": []})

    def test_two_lanes_racing_for_the_probe_get_exactly_one(self):
        self.open()
        os.environ["TARTCI_VM_DHCP_PROBE_SECS"] = "1"
        self.addCleanup(os.environ.pop, "TARTCI_VM_DHCP_PROBE_SECS", None)
        state = json.loads((self.tmp / "vm-dhcp" / "breaker.json").read_text())
        state["last_probe_at"] = 0
        (self.tmp / "vm-dhcp" / "breaker.json").write_text(json.dumps(state))
        env = {**os.environ}
        procs = [subprocess.Popen([sys.executable, "-B", str(BREAKER), "check", "--lane", f"l{i}"],
                                  stdout=subprocess.PIPE, text=True, env=env) for i in range(4)]
        actions = [json.loads(p.communicate()[0]) for p in procs]
        self.assertEqual(sorted(a["action"] for a in actions), ["backoff", "backoff", "backoff", "probe"])
        for loser in (a for a in actions if a["action"] == "backoff"):
            self.assertEqual(loser["events"], [])

    def test_bootpd_run_counter_moving_probes_at_once(self):
        self.open()
        self.runs.write_text("4")
        result = self.check(T0 + 70)
        self.assertEqual(result["action"], "probe")
        self.assertIn("trigger=bootpd_runs_moved", result["events"][0][1])

    def test_a_failed_probe_is_recorded_and_spent(self):
        self.open()
        self.check(T0 + 400, lane="a")
        result = self.record("no_ip", T0 + 600, lane="a")
        self.assertEqual(result["events"], [["vm_dhcp_probe", "lane=a vm=vm result=no_ip"]])
        self.assertEqual(self.state()["vms_spent"], 3)
        self.assertEqual(self.state()["state"], "open")

    def test_the_probe_s_address_closes_it_with_the_counts(self):
        self.open()
        self.check(T0 + 400, lane="a")
        result = self.record("ip", T0 + 470, lane="a")
        self.assertEqual(self.names(result), ["vm_dhcp_probe", "vm_dhcp_recovered"])
        self.assertIn("result=ip", result["events"][0][1])
        recovered = result["events"][1][1]
        for token in ("reason=probe", "open_s=410", "vms_spent=2", "probes=1", "latency_s=70"):
            self.assertIn(token, recovered)
        self.assertEqual(self.check(T0 + 471)["action"], "clone")

    def test_any_address_on_the_host_closes_it(self):
        self.open()
        result = self.record("ip", T0 + 90, lane="other")
        self.assertEqual(self.names(result), ["vm_dhcp_recovered"])
        self.assertIn("reason=boot_ok", result["events"][0][1])

    def test_a_reboot_after_it_opened_closes_it(self):
        self.open()
        os.environ["TARTCI_VM_DHCP_BOOT_TIME"] = str(T0 + 100)
        result = self.check(T0 + 120)
        self.assertEqual(result["action"], "clone")
        self.assertEqual(self.names(result), ["vm_dhcp_recovered"])
        self.assertIn("reason=host_reboot", result["events"][0][1])
        self.assertEqual(self.state()["state"], "closed")


class FailOpen(Case):
    def test_a_corrupt_breaker_reads_closed(self):
        directory = self.tmp / "vm-dhcp"
        directory.mkdir()
        (directory / "breaker.json").write_text("{not json")
        self.assertEqual(self.check(T0)["action"], "clone")
        (directory / "breaker.json").write_text(json.dumps({"state": "weird"}))
        self.assertEqual(self.check(T0)["action"], "clone")

    def test_writes_are_atomic_under_concurrent_reads(self):
        stop = threading.Event()
        bad: list[str] = []
        path = self.tmp / "vm-dhcp" / "breaker.json"

        def reader() -> None:
            while not stop.is_set():
                if path.exists():
                    try:
                        json.loads(path.read_text())
                    except ValueError as exc:
                        bad.append(str(exc))
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            for n in range(200):
                self.record("no_ip" if n % 3 else "ip", T0 + n)
        finally:
            stop.set()
            thread.join(timeout=10)
        self.assertEqual(bad, [])
        self.assertEqual(list((self.tmp / "vm-dhcp").glob(".breaker.*.tmp")), [])


class Shell(Case):
    def run_lib(self, body: str, extra: dict | None = None) -> subprocess.CompletedProcess:
        script = (f"TARTCI_ROOT={str(ROOT)!r}\nsource {str(LIB)!r}\n"
                  f"event(){{ printf '%s\\t%s\\n' \"$1\" \"$2\" >> {str(self.tmp / 'events')!r}; }}\n"
                  + body)
        # Real clock here: the host booted long before.
        env = {**os.environ, "TARTCI_VM_DHCP_BOOT_TIME": "1", **(extra or {})}
        return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                              env=env, check=False)

    def test_open_backs_off_and_closed_clones(self):
        out = self.run_lib("tartci_vm_dhcp_check l && echo clone\n")
        self.assertIn("clone", out.stdout)
        out = self.run_lib("tartci_vm_dhcp_record l no_ip v1; tartci_vm_dhcp_record l no_ip v2\n"
                           "rc=0; tartci_vm_dhcp_check l || rc=$?; echo \"rc=$rc backoff=$VM_DHCP_BACKOFF\"\n")
        self.assertIn("rc=75 backoff=1", out.stdout, out.stderr)
        self.assertIn("vm_dhcp_unanswered", (self.tmp / "events").read_text())

    def test_knob_off_reads_and_writes_nothing(self):
        out = self.run_lib("tartci_vm_dhcp_record l no_ip v1; tartci_vm_dhcp_record l no_ip v2\n"
                           "tartci_vm_dhcp_check l && echo clone\n",
                           {"TARTCI_VM_DHCP_BREAKER": "0"})
        self.assertIn("clone", out.stdout)
        self.assertFalse((self.tmp / "vm-dhcp").exists())
        self.assertFalse((self.tmp / "events").exists())

    def test_a_breaker_failure_boots(self):
        out = self.run_lib("tartci_vm_dhcp_check l && echo clone\n",
                           {"TARTCI_VM_DHCP_DIR": "/dev/null/vm-dhcp"})
        self.assertIn("clone", out.stdout, out.stderr)


class Wiring(unittest.TestCase):
    def test_the_breaker_is_checked_before_the_job_claim(self):
        body = RUNNER.read_text()
        run_one = body[body.index("run_one(){"):]
        self.assertLess(run_one.index("tartci_vm_dhcp_check"), run_one.index("tartci_job_claim_acquire"))
        self.assertLess(run_one.index("tartci_job_claim_acquire"),
                        run_one.index("tartci_assignment_v2_pre_clone_skip"))

    def test_both_boot_outcomes_are_recorded(self):
        body = RUNNER.read_text()
        no_ip = body.index('event boot_failed "no_ip"')
        self.assertIn("tartci_vm_dhcp_record", body[no_ip:no_ip + 300])
        self.assertIn('no_ip "$vm"', body[no_ip:no_ip + 300])
        got = body.index('CURRENT_IP="$ip"')
        self.assertIn('ip "$vm"', body[got:got + 200])

    def test_a_breaker_backoff_is_an_idle_pass(self):
        body = RUNNER.read_text()
        self.assertIn('[ "${VM_DHCP_BACKOFF:-0}" = 1 ]', body)

    @unittest.skipUnless(tomllib, "macos_fleet_lanes needs tomllib (Python 3.11+)")
    def test_the_profile_key_turns_it_off_and_nothing_else(self):
        import macos_fleet_lanes as fleet  # noqa: PLC0415
        base = (ROOT / "profiles" / "m1-macos-fleet.toml").read_text()
        with tempfile.TemporaryDirectory() as td:
            for value, expect in (("false", "0"), ("true", None)):
                path = Path(td) / f"{value}.toml"
                path.write_text(base.replace("[host]\n", f"[host]\nvm_dhcp_breaker = {value}\n", 1))
                envs = [__import__("plistlib").loads(b)["EnvironmentVariables"]
                        for b in fleet.rendered_plists(fleet.load(path)).values()]
                self.assertTrue(envs)
                self.assertEqual({e.get("TARTCI_VM_DHCP_BREAKER") for e in envs}, {expect})
            bad = Path(td) / "bad.toml"
            bad.write_text(base.replace("[host]\n", '[host]\nvm_dhcp_breaker = "no"\n', 1))
            result = subprocess.run([str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("vm_dhcp_breaker", result.stderr)


class Doctor(unittest.TestCase):
    def test_codes_and_the_root_remedy(self):
        reasons = fleet_doctor.load_reasons()
        for value, state, code in (({"state": "open", "opened_at": 1, "vms_spent": 2, "probes": 1},
                                    "problem", "vm_dhcp_unanswered"),
                                   ({"state": "closed"}, "ok", "vm_dhcp_ok"),
                                   ({"state": "unreadable", "error": "x"}, "unknown", "vm_dhcp_unreadable")):
            finding = fleet_doctor.check_vm_dhcp(value)
            self.assertEqual((finding.state, finding.code), (state, code))
            self.assertIn(code, fleet_doctor.CODES)
            self.assertIn(code, reasons)
        remedy = reasons["vm_dhcp_unanswered"]["remedy"]
        self.assertIn("sudo launchctl kickstart -k system/com.apple.bootpd", remedy)
        self.assertIn("tartci never runs it", remedy)


if __name__ == "__main__":
    unittest.main()
