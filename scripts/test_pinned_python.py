#!/usr/bin/env python3
"""The interpreter tartci's LaunchAgents run is pinned, never a PATH search.

On m5s (2026-10-15) the launchd watchdog had no TARTCI_PYTHON, so the picker
searched PATH and took /opt/homebrew/bin/python3, Homebrew 3.15. macOS denied
that binary Local Network, and every LAN probe failed with EHOSTUNREACH. The
installer now writes the pinned path to ~/.config/tartci/python.pin, the
picker uses it, and a tartci LaunchAgent with no pin refuses to run.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "providers" / "common" / "toml-python.lib.sh"
# The fake interpreters exec this one, and the picker accepts only a Python
# with tomllib (3.11+); the system Python 3.9 lane cannot stand in for them.
NEEDS_TOMLLIB = unittest.skipIf(sys.version_info < (3, 11), "needs tomllib (Python 3.11+)")


@NEEDS_TOMLLIB
class Picker(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.home = self.root / "home"
        (self.home / ".config" / "tartci").mkdir(parents=True)
        # The bait: a working python3.12 first on PATH, as Homebrew links one.
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.bait = self.interpreter(self.bin / "python3.12")
        self.pinned = self.interpreter(self.root / "pinned" / "python3.12")

    def interpreter(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        path.chmod(0o755)
        return path

    def pick(self, **env: str) -> subprocess.CompletedProcess:
        base = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin"}
        base.update(env)
        return subprocess.run(["/bin/bash", "-c", '. "$1"; tartci_toml_python_path', "_", str(LIB)],
                              capture_output=True, text=True, env=base, check=False)

    def pin(self, value: str) -> None:
        (self.home / ".config" / "tartci" / "python.pin").write_text(value + "\n")

    def test_the_pin_wins_over_a_path_search(self) -> None:
        self.pin(str(self.pinned))
        result = self.pick()
        self.assertEqual((result.returncode, result.stdout.strip()), (0, str(self.pinned)))

    def test_an_explicit_tartci_python_wins_over_the_pin(self) -> None:
        self.pin(str(self.pinned))
        result = self.pick(TARTCI_PYTHON=str(self.bait))
        self.assertEqual(result.stdout.strip(), str(self.bait))

    def test_a_tartci_launchagent_without_a_pin_refuses_the_path_search(self) -> None:
        result = self.pick(XPC_SERVICE_NAME="com.danielraffel.tartci.launchd-watchdog")
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stdout, "")
        self.assertIn("refusing an unpinned interpreter under launchd job "
                      "com.danielraffel.tartci.launchd-watchdog", result.stderr)

    def test_without_launchd_the_same_search_still_finds_the_bait(self) -> None:
        # Control, same instrument: an interactive shell keeps the search.
        result = self.pick()
        self.assertEqual((result.returncode, result.stdout.strip()), (0, str(self.bait)))
        other = self.pick(XPC_SERVICE_NAME="com.apple.Terminal")
        self.assertEqual(other.stdout.strip(), str(self.bait))

    def test_a_launchagent_with_a_pin_runs_the_pinned_binary(self) -> None:
        self.pin(str(self.pinned))
        result = self.pick(XPC_SERVICE_NAME="com.danielraffel.tartci.launchd-watchdog")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, str(self.pinned)))

    def test_a_vanished_pin_refuses_rather_than_falling_back(self) -> None:
        # A Homebrew cleanup removed the pinned Cellar path: fail loudly.
        self.pin(str(self.root / "gone" / "python3.12"))
        result = self.pick()
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stdout, "")
        self.assertIn("pinned interpreter", result.stderr)


class ProfilePython(unittest.TestCase):
    def read(self, text: str) -> subprocess.CompletedProcess:
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write(text)
        self.addCleanup(os.unlink, handle.name)
        return subprocess.run(["/bin/bash", "-c", '. "$1"; tartci_profile_python "$2"', "_",
                               str(LIB), handle.name], capture_output=True, text=True, check=False)

    def test_host_python_is_read_and_other_tables_are_ignored(self) -> None:
        result = self.read('schema = 1\n[host]\nid = "m5studio"\npython = "/opt/py/python3.12"\n'
                           '[[lane]]\npython = "/lane/python"\n')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "/opt/py/python3.12"))

    def test_a_profile_without_it_reads_nothing(self) -> None:
        result = self.read('[host]\nid = "m1"\n[[lane]]\npython = "/lane/python"\n')
        self.assertEqual((result.returncode, result.stdout), (1, ""))

    def test_the_validator_wants_an_absolute_path(self) -> None:
        try:
            import macos_fleet_lanes as lanes  # noqa: PLC0415 - tomllib (3.11+)
        except ModuleNotFoundError as exc:
            self.skipTest(str(exc))
        source = (ROOT / "profiles" / "m1-macos-fleet.toml").read_text()
        with tempfile.TemporaryDirectory() as tmp:
            for value, ok in (("/opt/py/python3.12", True), ("python3.12", False)):
                path = Path(tmp) / "p.toml"
                path.write_text(source.replace('ssh = "m1"\n', f'ssh = "m1"\npython = "{value}"\n', 1))
                if ok:
                    self.assertEqual(lanes.load(path)["host"]["python"], value)
                else:
                    with self.assertRaises(ValueError):
                        lanes.load(path)


@NEEDS_TOMLLIB
class SelfUpdateLauncher(unittest.TestCase):
    def test_the_launcher_is_built_with_the_profile_interpreter(self) -> None:
        try:
            import fleet_self_update as su  # noqa: PLC0415
        except ModuleNotFoundError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "p.toml"
            profile.write_text('[host]\nid = "m5studio"\npython = "/opt/py/python3.12"\n')
            self.assertEqual(su.launch_python(profile), "/opt/py/python3.12")
            profile.write_text('[host]\nid = "m5studio"\n')
            os.environ["TARTCI_RENDER_PYTHON"] = "/elsewhere/python3"
            try:
                self.assertEqual(su.launch_python(profile), "/elsewhere/python3")
            finally:
                del os.environ["TARTCI_RENDER_PYTHON"]


if __name__ == "__main__":
    unittest.main()
