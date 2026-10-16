#!/usr/bin/env python3
"""launcher_layout: which launcher a sealed-launcher host installs, v1 or v2."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import launcher_layout as ll

HERE = Path(__file__).resolve().parent
V1 = {"launch_helper": {"schema": 1}}
V2 = {"launch_helper": {"schema": 2, "layout": "v2"}}


class LauncherLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.home = Path(self.td.name)
        (self.home / ".config/tartci").mkdir(parents=True)
        self.opt_in = ll.opt_in_path(self.home)

    def tearDown(self) -> None:
        self.td.cleanup()

    def opt(self, mode: int = 0o600) -> None:
        self.opt_in.write_text("")
        os.chmod(self.opt_in, mode)

    def test_without_the_opt_in_a_host_stays_v1(self) -> None:
        self.assertEqual("v1", ll.decide(self.home, None))
        self.assertEqual("v1", ll.decide(self.home, V1))

    def test_the_opt_in_moves_a_v1_host_to_v2(self) -> None:
        self.opt()
        self.assertEqual("v2", ll.decide(self.home, V1))
        self.assertEqual("v2", ll.decide(self.home, None))
        self.assertEqual("v2", ll.decide(self.home, V2))

    def test_an_untrusted_opt_in_is_refused_loudly(self) -> None:
        for mode in (0o620, 0o602, 0o666):
            with self.subTest(mode=oct(mode)):
                self.opt(mode)
                with self.assertRaisesRegex(ll.LayoutError, "group- or world-writable"):
                    ll.decide(self.home, V1)
        self.opt_in.unlink()
        self.opt_in.mkdir()
        with self.assertRaisesRegex(ll.LayoutError, "regular file"):
            ll.decide(self.home, V1)
        self.opt_in.rmdir()
        target = self.home / "elsewhere"
        target.write_text("")
        os.chmod(target, 0o600)
        self.opt_in.symlink_to(target)
        with self.assertRaisesRegex(ll.LayoutError, "regular file"):
            ll.decide(self.home, V1)

    def test_an_opt_in_owned_by_another_user_is_refused(self) -> None:
        self.opt()
        real = os.getuid
        try:
            ll.os.getuid = lambda: real() + 1
            with self.assertRaisesRegex(ll.LayoutError, "not owned by this user"):
                ll.opted_in(self.home)
        finally:
            ll.os.getuid = real

    def test_a_v2_host_that_lost_its_opt_in_refuses_and_names_the_downgrade(self) -> None:
        with self.assertRaises(ll.LayoutError) as caught:
            ll.decide(self.home, V2)
        message = str(caught.exception)
        self.assertIn("never flips back silently", message)
        self.assertIn("--launcher-layout v1", message)
        self.assertIn("Removable Volumes", message)
        # The explicit downgrade is the way back.
        self.assertEqual("v1", ll.decide(self.home, V2, "v1"))

    def test_a_lost_receipt_does_not_read_as_nothing_installed(self) -> None:
        pointer = self.home / ".config/tartci/launcher-generation.json"
        pointer.write_text("{}")
        for receipt in (None, {"support": {}}):
            with self.subTest(receipt=receipt):
                self.assertEqual("v2", ll.installed(receipt, self.home))
                with self.assertRaises(ll.LayoutError) as caught:
                    ll.decide(self.home, receipt)
                self.assertIn("install receipt is missing but the launcher v2 pointer",
                              str(caught.exception))
                self.assertIn("--launcher-layout v1", str(caught.exception))
        self.assertEqual("v1", ll.decide(self.home, None, "v1"), "the explicit downgrade")
        self.opt()
        self.assertEqual("v2", ll.decide(self.home, None))
        # A recorded v1 receipt is not overruled by a leftover pointer.
        self.opt_in.unlink()
        self.assertEqual("v1", ll.decide(self.home, V1))

    def test_a_v2_app_on_disk_is_evidence_too(self) -> None:
        app = self.home / "TartCILauncher.app"
        (app / "Contents/Resources").mkdir(parents=True)
        self.assertIsNone(ll.installed(None, self.home, app), "control: no evidence")
        self.assertEqual("v1", ll.decide(self.home, None, app=app))
        (app / "Contents/Resources/policy.json").write_text(json.dumps({"schema": 2}))
        self.assertEqual("v2", ll.installed(None, self.home, app))
        with self.assertRaisesRegex(ll.LayoutError, "a launcher v2 app at"):
            ll.decide(self.home, None, app=app)
        (app / "Contents/Resources/policy.json").write_text(json.dumps({"schema": 1}))
        self.assertEqual("v1", ll.decide(self.home, None, app=app))
        status = ll.status(self.home, None, app)
        self.assertIsNone(status["layout"])

    def test_explicit_v2_needs_the_opt_in(self) -> None:
        with self.assertRaisesRegex(ll.LayoutError, "needs the per-host opt-in"):
            ll.decide(self.home, V1, "v2")
        self.opt()
        self.assertEqual("v2", ll.decide(self.home, V1, "v2"))
        with self.assertRaisesRegex(ll.LayoutError, "v1 or v2"):
            ll.decide(self.home, V1, "v3")

    def test_status_reports_the_layout_and_the_opt_in(self) -> None:
        self.assertEqual({"layout": None, "opt_in": "absent", "opt_in_path": str(self.opt_in),
                          "problem": None}, ll.status(self.home, None))
        self.assertEqual("v1", ll.status(self.home, V1)["layout"])
        self.opt()
        value = ll.status(self.home, V2)
        self.assertEqual(("v2", "present", None), (value["layout"], value["opt_in"], value["problem"]))
        self.opt(0o666)
        self.assertEqual("refused", ll.status(self.home, V1)["opt_in"])
        self.opt_in.unlink()
        self.assertIn("--launcher-layout v1", ll.status(self.home, V2)["problem"])
        self.assertIn("launcher problem", ll.status_text(ll.status(self.home, V2)))
        self.assertIn("layout none", ll.status_text(ll.status(self.home, None)))

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(HERE / "launcher_layout.py"),
                               "--home", str(self.home), *args],
                              capture_output=True, text=True, check=False)

    def test_the_command_reads_the_receipt_and_refuses_an_unreadable_one(self) -> None:
        receipt = self.home / "receipt.json"
        receipt.write_text(json.dumps(V2))
        result = self.cli("--receipt", str(receipt))
        self.assertEqual(2, result.returncode)
        self.assertIn("--launcher-layout v1", result.stderr)
        self.assertEqual("v2\n", self.cli("--receipt", str(receipt), "--installed").stdout)
        self.assertEqual("v1\n", self.cli("--receipt", str(receipt), "--explicit", "v1").stdout)
        self.assertEqual("v1\n", self.cli("--receipt", str(self.home / "absent.json")).stdout)
        receipt.write_text("{not json")
        result = self.cli("--receipt", str(receipt))
        self.assertEqual(2, result.returncode)
        self.assertIn("unreadable", result.stderr)
        status = json.loads(self.cli("--status").stdout)
        self.assertEqual("absent", status["opt_in"])


if __name__ == "__main__":
    unittest.main()
