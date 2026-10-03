#!/usr/bin/env python3
"""Runner credentials never ride in host argv, and runner binaries are pinned.

A JIT config is a single-use runner credential: whoever reads it first can
register as the runner and take the job. A value in an ssh command line is
readable by every local user through ps for the whole job, so the Linux
provider streams it over stdin like the macOS and Windows providers. The
Windows runner archive is downloaded into the same guest that receives that
credential, so it is pinned by SHA-256 exactly like the macOS archive.
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINUX = ROOT / "providers" / "tart-linux" / "runner.sh"
WINDOWS = ROOT / "providers" / "qemu-windows" / "runner.sh"
OPTIMIZE = ROOT / "providers" / "qemu-windows" / "optimize-golden.sh"


class LinuxJitTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.body = LINUX.read_text(encoding="utf-8")

    def test_jit_config_is_never_interpolated_into_a_command_line(self) -> None:
        self.assertNotIn("'$jit'", self.body)
        self.assertNotIn('"$jit" >', self.body)

    def test_jit_config_is_streamed_owner_only_then_removed_before_run(self) -> None:
        stream = self.body.index('printf \'%s\' "$jit" | ssh')
        owner_only = self.body.index("'umask 077 && cat > ~/jit.cfg'", stream)
        cleared = self.body.index('  jit=""', owner_only)
        removed = self.body.index("rm -f ~/jit.cfg", cleared)
        launch = self.body.index("./run.sh --jitconfig", removed)
        self.assertLess(stream, owner_only)
        self.assertLess(owner_only, cleared)
        self.assertLess(removed, launch)


class WindowsRunnerArchiveTests(unittest.TestCase):
    def test_runner_install_verifies_the_archive_before_extracting(self) -> None:
        for script in (WINDOWS, OPTIMIZE):
            with self.subTest(script=script.name):
                body = script.read_text(encoding="utf-8")
                download = body.index("actions-runner-win-arm64-$runnerVersion.zip")
                verify = body.index("Get-FileHash -Algorithm SHA256", download)
                extract = body.index("Expand-Archive", verify)
                self.assertLess(download, verify)
                self.assertLess(verify, extract)

    def test_runner_refuses_an_unpinned_download(self) -> None:
        body = WINDOWS.read_text(encoding="utf-8")
        refuse = body.index("refusing an unverified download")
        self.assertLess(refuse, body.index("Invoke-WebRequest -Uri $url"))

    def test_malformed_pins_are_rejected_before_any_guest_contact(self) -> None:
        env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
        for value in ("not-hex", "ab" * 31, "ab" * 33):
            with self.subTest(value=value):
                result = subprocess.run(
                    ["/bin/bash", str(OPTIMIZE)],
                    env={**env, "TARTCI_WIN_RUNNER_SHA256": value},
                    text=True, capture_output=True, timeout=30, check=False,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("64 hexadecimal characters", result.stderr)
        body = WINDOWS.read_text(encoding="utf-8")
        self.assertIn('die "TARTCI_WIN_RUNNER_SHA256 must contain 64 hexadecimal characters"', body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
