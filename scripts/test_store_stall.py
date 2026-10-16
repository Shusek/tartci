#!/usr/bin/env python3
"""store_stall: a bounded probe that names a stalled store volume."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_doctor as fd
import store_stall as ss

STORE = "/Volumes/Example/VMs"
REPO = "/Volumes/Example2/Code/pulp"
PROFILE = {"host": {"tart_home": STORE},
           "reclaim": {"repo": REPO, "worktrees_root": "/Volumes/Example2/Code/worktrees"}}


def fake_probe(path: str, argv: list[str]) -> dict:
    return {"volume": ss.volume_of(path), "path": path, "what": "fake", "argv": argv, "env": None}


class ProbeTests(unittest.TestCase):
    def test_a_hanging_command_is_reported_stalled_within_the_timeout(self) -> None:
        # A real child that hangs: the probe kills it and returns on time.
        hang = {"volume": "/Volumes/Example", "path": STORE, "what": "VM store listing",
                "argv": ["/bin/sleep", "30"], "env": None}
        started = time.monotonic()
        value = ss.probe([hang], timeout=1, ps_output="")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual("stalled", value["state"])
        detail = value["detail"]
        self.assertIn("/Volumes/Example: VM store listing did not finish within 1s", detail)
        self.assertIn("sandbox or TCC stall", detail)
        self.assertIn("reboot", detail)
        self.assertIn("tartci pool drain", detail)
        self.assertEqual("/Volumes/Example", value["stalled"][0]["volume"])

    def test_the_runner_kills_a_hanging_child_and_returns_on_time(self) -> None:
        started = time.monotonic()
        self.assertIsNone(ss.run_bounded(["/bin/sleep", "30"], 1))
        self.assertLess(time.monotonic() - started, 5)
        took = ss.run_bounded(["/bin/sleep", "0"], 5)
        self.assertIsNotNone(took)

    def test_a_command_that_answers_is_ok(self) -> None:
        # Control, same instrument: only the command's behaviour changed.
        value = ss.probe([{"volume": "/Volumes/Example", "path": STORE, "what": "listing",
                           "argv": ["/bin/sleep", "0"], "env": None}], timeout=5, ps_output="")
        self.assertEqual("ok", value["state"])
        self.assertEqual([], value["stalled"])

    def test_a_command_that_fails_fast_is_not_a_stall(self) -> None:
        value = ss.probe([{"volume": "/", "path": "/x", "what": "listing",
                           "argv": ["/bin/ls", "/nonexistent-store-path"], "env": None}],
                         timeout=5, ps_output="")
        self.assertEqual("ok", value["state"])

    def test_the_finding_says_how_long_an_older_process_has_been_hanging(self) -> None:
        ps = ("    05:00:12 /usr/bin/git -C /Volumes/Example2/Code/pulp rev-parse HEAD\n"
              "       00:40 /usr/bin/git -C /Volumes/Example2/Code/pulp status\n"
              " 1-02:00:00 /usr/bin/git -C /Users/someone/elsewhere status\n"
              "    09:00:00 /bin/zsh /Volumes/Example2/script.sh\n")
        never = lambda argv, timeout, env: None  # noqa: E731 - every probe hangs
        value = ss.probe([fake_probe(REPO, ["/usr/bin/git", "-C", REPO, "rev-parse"])],
                         timeout=1, runner=never, ps_output=ps)
        self.assertIn("/Volumes/Example2: fake did not finish within 1s; an older git on "
                      "it has been hanging for 5.0 h", value["detail"])
        self.assertEqual(5 * 3600 + 12, value["stalled"][0]["hanging_s"])

    def test_etime_parses_every_ps_form(self) -> None:
        for text, seconds in (("05", None), ("01:05", 65), ("02:01:05", 7265),
                              ("3-02:01:05", 3 * 86400 + 7265)):
            with self.subTest(text=text):
                self.assertEqual(seconds, ss.etime_seconds(text))

    def test_targets_cover_the_vm_store_and_the_repository_volumes(self) -> None:
        found = ss.targets(PROFILE)
        whats = {(item["volume"], item["what"]) for item in found}
        self.assertIn(("/Volumes/Example", "VM store listing"), whats)
        self.assertIn(("/Volumes/Example2", "git rev-parse"), whats)
        self.assertIn(("/Volumes/Example2", "worktrees listing"), whats)
        self.assertEqual([], ss.targets({}))

    def test_status_reads_the_installed_profile(self) -> None:
        # Any TOML parser will do here: the fixture profile is JSON, which a
        # stand-in parser reads, so this runs on the hosts' Python too.
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self.assertEqual("unprobed", ss.status(home)["state"])   # no profile yet
            path = ss.installed_profile(home)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"host": {"tart_home": str(home / "store")}}))
            with mock.patch.object(ss, "tomllib", types.SimpleNamespace(loads=json.loads)):
                value = ss.status(home, timeout=5, ps_output="")
            self.assertEqual("ok", value["state"])
            with mock.patch.object(ss, "tomllib", None):
                self.assertEqual("unprobed", ss.status(home)["state"])


class DoctorTests(unittest.TestCase):
    def test_each_state_has_its_finding(self) -> None:
        stalled = ss.probe([{"volume": "/Volumes/Example", "path": STORE, "what": "listing",
                             "argv": ["x"], "env": None}], timeout=1,
                           runner=lambda argv, timeout, env: None, ps_output="")
        for value, state, code in ((stalled, fd.PROBLEM, "store_stalled"),
                                   ({"state": "ok", "detail": "1 probe", "stalled": []},
                                    fd.OK, "store_ok"),
                                   (None, fd.UNKNOWN, "store_unprobed")):
            with self.subTest(code=code):
                finding = fd.check_store_stall(value)
                self.assertEqual((state, code), (finding.state, finding.code))
                self.assertIn(code, fd.CODES)
        self.assertIn("/Volumes/Example", fd.check_store_stall(stalled).detail)

    def test_pool_status_reports_the_store_in_json_and_text(self) -> None:
        import os  # noqa: PLC0415
        import subprocess  # noqa: PLC0415
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home, "TARTCI_HOME": f"{home}/.tartci"}
            run = lambda *a: subprocess.run(  # noqa: E731
                ["bash", str(root / "tartci"), "pool", "status", *a], cwd=root, env=env,
                text=True, capture_output=True, timeout=120, check=False)
            value = json.loads(run("--json").stdout)
            text = run().stdout
        self.assertEqual("unprobed", value["store"]["state"])
        self.assertIn("store: not probed (no installed fleet profile)", text)

    def test_the_lane_path_never_imports_the_probe(self) -> None:
        # Doctor and pool status only: no provider or lane script runs it.
        root = Path(__file__).resolve().parents[1]
        users = sorted(str(path.relative_to(root))
                       for path in [*root.glob("providers/**/*.sh"), *root.glob("scripts/*.py"),
                                    root / "tartci"]
                       if path.is_file() and "store_stall" in path.read_text()
                       and not path.name.startswith("test_") and path.name != "store_stall.py")
        self.assertEqual(["scripts/fleet_doctor.py", "tartci"], users)


if __name__ == "__main__":
    unittest.main()
