#!/usr/bin/env python3
"""The disk write-latency watchdog, driven through `tartci disk watch`.

A fake probe stands in for the filesystem: it reads what to do (`fast`,
`slow <seconds>` or `hang`) from a control file and logs every invocation, so
each test shapes the volume's latency and counts the probes started. Issue
writes are never tried from a scratch TARTCI_HOME; the alert state shows the
episode went through the alert path. The issue text and its close are checked
in-process with a fake opener.

Python 3.9-safe.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disk_watchdog as dw  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]

FAKE_PROBE = """
import pathlib, sys, time
control = pathlib.Path(__file__).with_name("control")
log = pathlib.Path(__file__).with_name("probes.log")
with log.open("a") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
mode = control.read_text().split() if control.exists() else ["fast"]
if mode[0] == "slow":
    time.sleep(float(mode[1]))
elif mode[0] == "hang":
    time.sleep(3600)
print("0.001")
"""


class Harness:
    def __init__(self, test: unittest.TestCase) -> None:
        self.temp = tempfile.TemporaryDirectory()
        test.addCleanup(self.temp.cleanup)
        base = pathlib.Path(self.temp.name)
        self.home = base / "tartci-home"
        self.volume = base / "volume"
        self.volume.mkdir()
        self.fake = base / "fake"
        self.fake.mkdir()
        (self.fake / "probe.py").write_text(FAKE_PROBE)
        self.state = self.home / "state" / "disk-watchdog"

    def mode(self, text: str) -> None:
        (self.fake / "control").write_text(text)

    def probes(self) -> int:
        log = self.fake / "probes.log"
        return len(log.read_text().splitlines()) if log.exists() else 0

    def env(self) -> dict:
        env = dict(os.environ)
        env.update({
            "TARTCI_HOME": str(self.home),
            "TARTCI_DISK_WATCH_PATHS": str(self.volume),
            "TARTCI_DISK_PROBE_COMMAND": json.dumps([sys.executable, str(self.fake / "probe.py")]),
            "TARTCI_DISK_PROBE_TIMEOUT_S": "2",
            "TARTCI_DISK_EVIDENCE_TIMEOUT_S": "5",
        })
        env.pop("TARTCI_DISK_WATCH_ISSUE", None)
        return env

    def watch(self) -> dict:
        proc = subprocess.run([str(ROOT / "tartci"), "disk", "watch", "--json"],
                              env=self.env(), capture_output=True, text=True, timeout=120,
                              check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout)

    def alert_state(self) -> dict:
        found = list(self.state.glob("alert-*.json"))
        return json.loads(found[0].read_text()) if found else {}

    def events(self) -> list:
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["event"] for line in path.read_text().splitlines()]


class ThroughTheEntryPoint(unittest.TestCase):
    def test_a_fast_volume_is_quiet(self) -> None:
        h = Harness(self)
        h.mode("fast")
        result = h.watch()
        row = result["paths"][0]
        self.assertEqual(row["path"], str(h.volume))
        self.assertEqual(row["probe"]["status"], "ok")
        self.assertEqual(h.probes(), 1)
        self.assertEqual(h.events(), [])
        self.assertFalse((h.state / "evidence").exists())

    def test_a_slow_write_opens_one_episode_with_evidence_once(self) -> None:
        h = Harness(self)
        h.mode("slow 1.5")
        first = h.watch()["paths"][0]
        self.assertEqual(first["probe"]["status"], "slow")
        self.assertGreater(first["probe"]["elapsed_s"], 1.0)
        self.assertTrue(first["episode_since"])
        evidence = pathlib.Path(first["evidence"])
        for name in ("log-show.txt", "ps-blocked.txt", "evidence.json"):
            self.assertTrue((evidence / name).exists(), name)
        summary = json.loads((evidence / "evidence.json").read_text())
        self.assertEqual(summary["path"], str(h.volume))
        self.assertEqual(summary["probe"]["status"], "slow")
        self.assertEqual(h.events(), ["disk_latency_slow"])
        # The episode went through the alert path. A state dir under the temp
        # dir never writes an issue, so none was tried.
        alert = h.alert_state()
        self.assertEqual(alert.get("since"), first["episode_since"])
        self.assertTrue(alert.get("evented"))
        self.assertNotIn("issue", alert)
        self.assertNotIn("issue_error", alert)

        mtime = (evidence / "evidence.json").stat().st_mtime
        second = h.watch()["paths"][0]
        self.assertEqual(second["episode_since"], first["episode_since"])
        self.assertEqual(second["evidence"], first["evidence"])
        self.assertEqual((evidence / "evidence.json").stat().st_mtime, mtime)
        self.assertEqual(h.events(), ["disk_latency_slow"])
        self.assertEqual(len(list((h.state / "evidence").iterdir())), 1)

    def test_a_write_that_never_finishes_times_out_without_waiting(self) -> None:
        h = Harness(self)
        h.mode("hang")
        start = time.monotonic()
        row = h.watch()["paths"][0]
        self.assertLess(time.monotonic() - start, 60)
        self.assertEqual(row["probe"]["status"], "timeout")
        self.assertFalse(row["probe"]["stuck"], row["probe"])  # SIGKILL took it
        self.assertFalse(dw._alive(row["probe"]["pid"]))
        self.assertTrue(row["episode_since"])
        self.assertEqual(h.events(), ["disk_latency_slow"])
        evidence = pathlib.Path(row["evidence"])
        self.assertTrue((evidence / f"sample-{row['probe']['pid']}.txt").exists())
        state = json.loads(next(h.state.glob("path-*.json")).read_text())
        self.assertNotIn("outstanding", state)

    def test_three_fast_probes_end_the_episode(self) -> None:
        h = Harness(self)
        h.mode("slow 1.5")
        h.watch()
        h.mode("fast")
        # Three fast probes in a row end it, not fewer.
        for _ in range(2):
            row = h.watch()["paths"][0]
            self.assertTrue(row["episode_since"], "closed too early")
        row = h.watch()["paths"][0]
        self.assertIsNone(row["episode_since"])
        self.assertTrue(row.get("closed"))
        self.assertEqual(h.events(), ["disk_latency_slow", "disk_latency_recovered"])
        self.assertEqual(h.alert_state(), {})

    def test_a_probe_still_in_the_kernel_is_not_joined_by_another(self) -> None:
        # A probe that outlived SIGKILL is recorded as outstanding. Stand one up
        # (alive, carrying the probe marker) and point the state at it.
        h = Harness(self)
        h.mode("fast")
        stuck = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                                  dw.PROBE_MARKER, str(h.volume)])
        self.addCleanup(stuck.wait)
        self.addCleanup(stuck.kill)
        h.state.mkdir(parents=True)
        (h.state / f"path-{dw._key(str(h.volume))}.json").write_text(json.dumps(
            {"outstanding": {"pid": stuck.pid, "started": time.time() - 300}}))
        row = h.watch()["paths"][0]
        self.assertEqual(h.probes(), 0, "a second probe was started beside a stuck one")
        self.assertEqual(row["probe"]["status"], "timeout")
        self.assertTrue(row["probe"]["stuck"])
        self.assertGreaterEqual(row["probe"]["elapsed_s"], 300)
        self.assertTrue(row["episode_since"])
        # Once it is gone, probing resumes.
        stuck.kill()
        stuck.wait()
        row = h.watch()["paths"][0]
        self.assertEqual(h.probes(), 1)
        self.assertEqual(row["probe"]["status"], "ok")

    def test_status_reads_the_last_probe(self) -> None:
        h = Harness(self)
        h.mode("slow 1.5")
        h.watch()
        proc = subprocess.run([str(ROOT / "tartci"), "disk", "status", "--json"], env=h.env(),
                              capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = json.loads(proc.stdout)["paths"][0]
        self.assertEqual(row["last"]["status"], "slow")
        self.assertTrue(row["since"])
        self.assertTrue(row["evidence"])


class Episodes(unittest.TestCase):
    """The issue itself, with a fake opener, in-process."""

    def test_one_issue_per_episode_closed_on_recovery(self) -> None:
        h = Harness(self)
        env = h.env()
        for key in ("TARTCI_HOME", "TARTCI_DISK_WATCH_PATHS", "TARTCI_DISK_PROBE_COMMAND",
                    "TARTCI_DISK_PROBE_TIMEOUT_S", "TARTCI_DISK_EVIDENCE_TIMEOUT_S"):
            self.addCleanup(os.environ.pop, key, None)
            os.environ[key] = env[key]
        opened, closed = [], []

        def issue(title: str, body: str):
            opened.append((title, body))
            return 0, "41"

        def close(number: str):
            closed.append(number)
            return 0, "closed"

        def run(command, timeout, out):
            out.write_text("fake\n")
            return "ok", "PID PPID STAT ELAPSED WCHAN COMMAND\n"

        h.mode("slow 1.5")
        kw = dict(issue=issue, close=close, run=run, host="m5studio", target="tartci-m5studio")
        for _ in range(2):
            dw.watch_path(str(h.volume), h.state, time.time(), **kw)
        self.assertEqual(len(opened), 1)
        title, body = opened[0]
        self.assertIn("m5studio", title)
        self.assertIn(str(h.volume), title)
        self.assertIn("tartci disk status", body)
        self.assertIn("stuck_in_open", body)
        h.mode("fast")
        for _ in range(2):
            dw.watch_path(str(h.volume), h.state, time.time(), **kw)
        self.assertEqual(closed, [], "closed before the third fast probe")
        dw.watch_path(str(h.volume), h.state, time.time(), **kw)
        self.assertEqual(closed, ["41"])


class Pieces(unittest.TestCase):
    def test_blocked_processes_are_d_or_u_state(self) -> None:
        ps = "\n".join([
            "  PID  PPID STAT ELAPSED WCHAN  COMMAND",
            "  101     1 Ss   01:00   -      /sbin/launchd",
            "  202     1 U    07:00:00 -     python3 -c probe /Volumes/Store",
            "  303     1 D+   00:05   -      cp a b",
            "  404     1 R    00:01   -      ps",
        ])
        self.assertEqual(dw.blocked_pids(ps), [202, 303])

    def test_a_stack_in_open_is_recognised(self) -> None:
        self.assertTrue(dw.in_open("    + 2381 __open  (in libsystem_kernel.dylib) + 8"))
        self.assertTrue(dw.in_open("open$NOCANCEL  (in libsystem_kernel.dylib)"))
        self.assertFalse(dw.in_open("    + 2381 __read_nocancel  (in libsystem_kernel.dylib)"))

    def test_evidence_samples_the_stuck_probe_and_the_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def run(command, timeout, out):
                calls.append(list(command))
                if command[0] == "/bin/ps":
                    text = "PID PPID STAT\n  77 1 U\n  88 1 S\n"
                elif command[0] == "/usr/bin/sample":
                    text = "__open" if command[1] == "55" else "__read"
                else:
                    text = "log"
                out.write_text(text)
                return "ok", text

            summary = dw.capture_evidence(pathlib.Path(tmp) / "e", "/Volumes/X",
                                          {"status": "timeout", "pid": 55}, run=run)
            self.assertEqual(summary["sampled"], [55, 77])
            self.assertEqual(summary["stuck_in_open"], [55])
            self.assertEqual(summary["blocked_pids"], [77])
            self.assertEqual(calls[0][0], "/usr/bin/log")
            self.assertIn("--last", calls[0])
            blocked = (pathlib.Path(tmp) / "e" / "ps-blocked.txt").read_text()
            self.assertIn("77", blocked)
            self.assertNotIn(" 88 ", blocked)

    def test_a_capture_that_hangs_is_abandoned_within_its_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            start = time.monotonic()
            outcome, _ = dw.bounded([sys.executable, "-c", "import time; time.sleep(60)"],
                                    1.0, pathlib.Path(tmp) / "out.txt")
            self.assertLess(time.monotonic() - start, 10)
            # Killed and reaped, not left running: only a process stuck in the
            # kernel is abandoned.
            self.assertEqual(outcome, "timed out after 1 s")

    def test_placeholder_paths_are_dropped(self) -> None:
        os.environ["TARTCI_DISK_WATCH_PATHS"] = os.pathsep.join(
            ["/Volumes/A", "$DISK_WATCH_PATHS", "/Volumes/A", "/Users/x/.cache"])
        self.addCleanup(os.environ.pop, "TARTCI_DISK_WATCH_PATHS", None)
        self.assertEqual(dw.watch_paths(), ["/Volumes/A", "/Users/x/.cache"])

    def test_the_agent_template_runs_the_pass_every_minute(self) -> None:
        import plistlib
        import re
        source = (ROOT / "launchd" / "com.danielraffel.tartci.disk-watchdog.plist.template").read_bytes()
        value = plistlib.loads(re.sub(rb"<!--.*?-->", b"", source, flags=re.DOTALL))
        self.assertEqual(value["ProgramArguments"][-2:], ["disk", "watch"])
        self.assertEqual(value["StartInterval"], 60)
        self.assertEqual(value["EnvironmentVariables"]["TARTCI_DISK_WATCH_PATHS"],
                         "$DISK_WATCH_PATHS")


if __name__ == "__main__":
    unittest.main()
