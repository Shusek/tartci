#!/usr/bin/env python3
"""Behavioral regressions for the sealed resident launcher app."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native/macos/tartci-launcher/main.swift"


@unittest.skipUnless(os.uname().sysname == "Darwin", "macOS native launcher")
class LauncherRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.app = cls.root / "TartCILauncher.app"
        cls.launcher = cls.app / "Contents/MacOS/tartci-launcher"
        support = cls.app / "Contents/Resources/support"
        cls.launcher.parent.mkdir(parents=True)
        support.mkdir(parents=True)
        cls.pid_file = cls.root / "descendant.pid"
        lanes = {}
        for lane, mode in (
            ("exit-zero", "exit-zero"),
            ("exit-one", "exit-one"),
            ("spawn-and-exit", "spawn-and-exit"),
            ("term-group", "term-group"),
        ):
            lanes[lane] = {"environment": {
                "HOME": str(cls.root),
                "PATH": "/usr/bin:/bin",
                "TART_HOME": "/Volumes/Workshop/VMs",
                "TARTCI_TEST_MODE": mode,
                "TARTCI_TEST_PID_FILE": str(cls.pid_file),
            }}
        (cls.app / "Contents/Resources/bundle.json").write_text(
            json.dumps({"tart_home": "/Volumes/Workshop/VMs"})
        )
        (cls.app / "Contents/Resources/lanes.json").write_text(
            json.dumps({"schema": 1, "lanes": lanes})
        )
        launch = support / ".tartci-launch"
        launch.write_text(
            "#!/bin/sh\n"
            "case \"$TARTCI_TEST_MODE\" in\n"
            "  exit-zero) exit 0 ;;\n"
            "  exit-one) exit 1 ;;\n"
            "  spawn-and-exit) trap '' TERM; while :; do sleep 1; done & echo $! > \"$TARTCI_TEST_PID_FILE\"; exit 0 ;;\n"
            "  term-group) sh -c 'trap \"\" TERM INT HUP; while :; do sleep 1; done' & echo $! > \"$TARTCI_TEST_PID_FILE\"; trap 'exit 0' TERM INT HUP; while :; do sleep 1; done ;;\n"
            "  *) exit 99 ;;\n"
            "esac\n"
        )
        launch.chmod(0o755)
        subprocess.run([
            "xcrun", "swiftc", "-O", "-D", "TARTCI_TESTING",
            "-framework", "Security", str(SOURCE), "-o", str(cls.launcher),
        ], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def setUp(self) -> None:
        self.pid_file.unlink(missing_ok=True)

    def run_lane(self, lane: str, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.launcher), "--lane", lane], check=False, **kwargs
        )

    def test_propagates_child_exit_and_discards_ambient_policy(self) -> None:
        poisoned = dict(os.environ)
        poisoned.update({
            "TARTCI_TEST_MODE": "exit-one",
            "PULP_UNTRUSTED": "1",
            "SHIPYARD_UNTRUSTED": "1",
            "PATH": "/tmp/not-real",
        })
        self.assertEqual(self.run_lane("exit-zero", env=poisoned).returncode, 0)
        self.assertEqual(self.run_lane("exit-one").returncode, 1)

    def test_rejects_arbitrary_execution_and_unknown_lane(self) -> None:
        self.assertEqual(subprocess.run([
            str(self.launcher), "--", "/usr/bin/true",
        ], check=False).returncode, 64)
        self.assertEqual(self.run_lane("not-signed").returncode, 77)
        self.assertEqual(subprocess.run([
            str(self.launcher), "--probe-store", "/tmp",
        ], check=False).returncode, 64)

    def test_kills_descendant_after_direct_child_exits(self) -> None:
        result = self.run_lane("spawn-and-exit", timeout=5)
        self.assertEqual(result.returncode, 0)
        descendant = int(self.pid_file.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail(f"descendant {descendant} survived launcher exit")

    def test_term_kills_full_child_group(self) -> None:
        launcher = subprocess.Popen([
            str(self.launcher), "--lane", "term-group",
        ])
        try:
            deadline = time.monotonic() + 3
            while not self.pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.pid_file.exists())
            descendant = int(self.pid_file.read_text())
            launcher.send_signal(signal.SIGTERM)
            self.assertEqual(launcher.wait(timeout=5), 0)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(descendant, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"descendant {descendant} survived TERM cleanup")
        finally:
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait()




V2_TEAM = "AB12CD34EF"
V2_STORE = "/Volumes/Example/VMs"
V2_TEST_REQUIREMENT = 'identifier "com.danielraffel.tartci.launch-set"'
V2_ENTRY = (
    "#!/bin/sh\n"
    "case \"$TARTCI_TEST_MODE\" in\n"
    "  exit-zero) exit 0 ;;\n"
    "  exit-one) exit 1 ;;\n"
    "  *) exit 99 ;;\n"
    "esac\n"
)


@unittest.skipUnless(os.uname().sysname == "Darwin", "macOS native launcher")
class LauncherV2RuntimeTests(unittest.TestCase):
    """Launcher v2 runs only a signed, intact, trusted launch set; otherwise 77.

    The test build accepts an ad-hoc signature by its identifier
    (TARTCI_TEST_LAUNCH_SET_REQUIREMENT); production pins policy.team_id."""

    @classmethod
    def setUpClass(cls) -> None:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import launch_set
        import test_launch_set
        cls.ls, cls.fixture = launch_set, test_launch_set
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.launcher = cls.base / "TartCILauncher.app/Contents/MacOS/tartci-launcher"
        cls.launcher.parent.mkdir(parents=True)
        subprocess.run(["xcrun", "swiftc", "-O", "-D", "TARTCI_TESTING", "-framework", "Security",
                        str(SOURCE), "-o", str(cls.launcher)], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup)
        work = Path(self.td.name)
        os.chmod(work, 0o755)
        self.root = work / "generations"
        self.root.mkdir(mode=0o755)
        self.pointer = work / "launcher-generation.json"
        resources = self.launcher.parent.parent / "Resources"
        resources.mkdir(exist_ok=True)
        (resources / "policy.json").write_text(json.dumps({
            "schema": 2, "identifier": "com.danielraffel.tartci.launcher", "team_id": V2_TEAM,
            "tart_home": V2_STORE, "generations_root": str(self.root),
            "pointer": str(self.pointer)}))
        self.generation = self.fixture.make_generation(self.root)
        (self.generation / self.ls.ENTRYPOINT).write_text(V2_ENTRY)
        self.lanes = {mode: {"environment": {"HOME": str(work), "PATH": "/usr/bin:/bin",
                                             "TART_HOME": V2_STORE, "TARTCI_TEST_MODE": mode}}
                      for mode in ("exit-zero", "exit-one")}
        self.seal()

    def _cleanup(self) -> None:
        for dirpath, dirs, _files in os.walk(self.td.name):
            for name in dirs:
                os.chmod(os.path.join(dirpath, name), 0o755)
        self.td.cleanup()

    def seal(self, *, identifier: str = "com.danielraffel.tartci.launch-set", value=None) -> None:
        """Write, ad-hoc sign and freeze the launch set; point at it."""
        os.chmod(self.generation, 0o755)
        for name in (self.ls.SET_NAME, self.ls.SIG_NAME):
            (self.generation / name).unlink(missing_ok=True)
        self.ls.write(self.generation, value or self.ls.build(self.generation, self.lanes,
                                                              tart_home=V2_STORE))
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", identifier,
                        "--detached", str(self.generation / self.ls.SIG_NAME),
                        str(self.generation / self.ls.SET_NAME)], check=True, capture_output=True)
        self.ls.freeze(self.generation)
        self.point(self.generation)

    def point(self, generation) -> None:
        self.pointer.write_text(json.dumps({"schema": 1, "generation": str(generation)}))
        os.chmod(self.pointer, 0o600)

    def run_lane(self, lane: str = "exit-zero") -> subprocess.CompletedProcess:
        env = {"PATH": "/usr/bin:/bin", "TARTCI_TEST_LAUNCH_SET_REQUIREMENT": V2_TEST_REQUIREMENT}
        return subprocess.run([str(self.launcher), "--lane", lane], env=env, capture_output=True,
                              text=True, timeout=60, check=False)

    def edit(self, relative: str, data: bytes) -> None:
        path = self.generation / relative
        mode = path.stat().st_mode & 0o777
        os.chmod(path.parent, 0o755)
        os.chmod(path, 0o644)
        path.write_bytes(data)
        os.chmod(path, mode)
        os.chmod(path.parent, 0o555)

    def assertRefused(self, why: str) -> None:
        result = self.run_lane()
        self.assertEqual(77, result.returncode, result.stderr)
        self.assertIn(why, result.stderr)

    def test_a_valid_set_runs_its_lane(self) -> None:
        self.assertEqual(0, self.run_lane("exit-zero").returncode, self.run_lane().stderr)
        self.assertEqual(1, self.run_lane("exit-one").returncode)
        result = self.run_lane("not-in-set")
        self.assertEqual(77, result.returncode)
        self.assertIn("not in the signed launch set", result.stderr)

    def test_a_set_signed_with_another_identifier_is_refused(self) -> None:
        self.seal(identifier="com.example.other")
        self.assertRefused("signature is invalid")

    def test_an_unsigned_set_is_refused(self) -> None:
        os.chmod(self.generation, 0o755)
        (self.generation / self.ls.SIG_NAME).unlink()
        os.chmod(self.generation, 0o555)
        self.assertRefused("cannot open")

    def test_a_set_edited_after_signing_is_refused(self) -> None:
        value = json.loads((self.generation / self.ls.SET_NAME).read_text())
        value["lanes"]["exit-zero"]["environment"]["TARTCI_TEST_MODE"] = "exit-one"
        os.chmod(self.generation, 0o755)
        self.edit(self.ls.SET_NAME, (json.dumps(value) + "\n").encode())
        self.assertRefused("signature is invalid")

    def test_one_byte_in_any_member_or_the_entrypoint_is_refused(self) -> None:
        for relative, why in (("tartci", "does not match its digest"),
                              ("scripts/x.py", "does not match its digest"),
                              (self.ls.ENTRYPOINT, "does not match its digest"),
                              (self.ls.MANIFEST_NAME, "support manifest does not match")):
            with self.subTest(relative=relative):
                original = (self.generation / relative).read_bytes()
                self.edit(relative, original + b"#")
                self.assertRefused(why)
                self.edit(relative, original)
                self.assertEqual(0, self.run_lane().returncode)

    def test_a_writable_or_foreign_path_is_refused(self) -> None:
        member = self.generation / "scripts/x.py"
        for path, mode in ((member, 0o464), (member.parent, 0o575), (self.generation, 0o575),
                           (self.root, 0o775), (self.pointer, 0o620)):
            with self.subTest(path=str(path), mode=oct(mode)):
                before = path.stat().st_mode & 0o777
                os.chmod(path, mode)
                self.assertRefused("writable" if path != self.pointer else "trusted")
                os.chmod(path, before)
        self.assertEqual(0, self.run_lane().returncode)

    def test_a_symlinked_member_is_refused(self) -> None:
        os.chmod(self.generation, 0o755)
        os.chmod(self.generation / "scripts", 0o755)
        real = self.generation / "scripts/x.py"
        moved = self.generation / "scripts/real.py"
        os.rename(real, moved)
        real.symlink_to(moved)
        self.ls.freeze(self.generation)
        self.assertRefused("symlink")

    def test_a_pointer_outside_the_root_is_refused(self) -> None:
        elsewhere = Path(self.td.name) / "elsewhere"
        elsewhere.mkdir()
        for target in (elsewhere, self.root, self.root / ".staging-x", Path("/usr/bin")):
            with self.subTest(target=str(target)):
                self.point(target)
                self.assertRefused("pointer names no generation")

    def test_a_signed_lane_on_another_store_is_refused(self) -> None:
        value = self.ls.build(self.generation, self.lanes, tart_home=V2_STORE)
        value["lanes"]["exit-zero"]["environment"]["TART_HOME"] = "/Volumes/Other/VMs"
        self.seal(value=value)
        self.assertRefused("wrong Tart store")



@unittest.skipUnless(os.uname().sysname == "Darwin", "macOS native launcher")
class LauncherV2ProductionTests(LauncherV2RuntimeTests):
    """The production build: the requirement is the policy's team, and the
    test override is compiled out, so an ad-hoc launch set never runs."""

    @classmethod
    def setUpClass(cls) -> None:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import launch_set
        import test_launch_set
        cls.ls, cls.fixture = launch_set, test_launch_set
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        app = cls.base / "TartCILauncher.app"
        cls.launcher = app / "Contents/MacOS/tartci-launcher"
        cls.launcher.parent.mkdir(parents=True)
        (app / "Contents/Resources").mkdir()
        subprocess.run(["xcrun", "swiftc", "-O", "-framework", "Security",
                        str(SOURCE), "-o", str(cls.launcher)], check=True)
        (app / "Contents/Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
            "<key>CFBundleExecutable</key><string>tartci-launcher</string>"
            "<key>CFBundleIdentifier</key><string>com.example.tartci.launcher-test</string>"
            "<key>CFBundlePackageType</key><string>APPL</string></dict></plist>")
        cls.app = app

    def setUp(self) -> None:
        super().setUp()
        # policy.json is written by the parent setUp; the app is sealed after it.
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(self.app)],
                       check=True, capture_output=True)

    def test_an_ad_hoc_launch_set_is_refused_even_with_the_test_override(self) -> None:
        self.assertRefused("not from the pinned team")

    # The parent's tests assume the test requirement; in production every
    # ad-hoc set is refused before any of their specific checks.
    test_a_valid_set_runs_its_lane = None
    test_a_set_signed_with_another_identifier_is_refused = None
    test_an_unsigned_set_is_refused = None
    test_a_set_edited_after_signing_is_refused = None
    test_one_byte_in_any_member_or_the_entrypoint_is_refused = None
    test_a_writable_or_foreign_path_is_refused = None
    test_a_symlinked_member_is_refused = None
    test_a_pointer_outside_the_root_is_refused = None
    test_a_signed_lane_on_another_store_is_refused = None

if __name__ == "__main__":
    unittest.main()
