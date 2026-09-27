#!/usr/bin/env python3
"""The disk reclaimer must be installed by setup, not left to a manual step.

Three hosts ran without it because installing it was documented rather than
wired: m5 reached 14 GiB free with 488 build dirs and refused 276 leases. These
tests pin the two properties that keep that from recurring — `setup` reaches the
installer, and the installer is safe to call on every setup.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_reclaim_agent.sh"
LABEL = "com.danielraffel.tartci.reclaim"


def run(argv, env=None):
    return subprocess.run(argv, cwd=ROOT, text=True, capture_output=True,
                          env={**os.environ, **(env or {})}, check=False)


class SetupWiresTheReclaimer(unittest.TestCase):
    def test_setup_invokes_the_installer(self):
        # The gap was never "no installer" — it was that nothing called one. A
        # grep is the whole assertion: if setup stops calling it, a new machine
        # silently ships without the janitor again.
        body = (ROOT / "tartci").read_text()
        start = body.index("cmd_setup()")
        end = body.index("cmd_bench()", start)
        self.assertIn("install_reclaim_agent.sh", body[start:end],
                      "cmd_setup must install the disk reclaimer")

    def test_doctor_reports_both_janitors(self):
        body = (ROOT / "tartci").read_text()
        start = body.index("cmd_doctor()")
        end = body.index("cmd_setup()", start)
        section = body[start:end]
        self.assertIn("janitors:", section)
        self.assertIn("com.danielraffel.tartci.${_j%%:*}", section)


def launchctl_double(directory: Path) -> tuple[Path, Path]:
    """A launchctl stand-in that records every call and holds no real domain."""
    calls = directory / "launchctl.calls"
    double = directory / "launchctl-double"
    double.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{calls}'\n"
        f"case \"$1\" in print) [ -f '{directory}/loaded' ] ;; "
        f"bootstrap) touch '{directory}/loaded' ;; bootout) rm -f '{directory}/loaded' ;; "
        "esac\n")
    double.chmod(0o755)
    return double, calls


class InstallerIsSafeToRepeat(unittest.TestCase):
    def test_plan_mode_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "agents"
            double, _ = launchctl_double(Path(td))
            res = run([str(INSTALLER), "--plan"], {"TARTCI_AGENTS_DIR": str(agents),
                                                   "TARTCI_LAUNCHCTL_BIN": str(double)})
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertFalse((agents / f"{LABEL}.plist").exists(),
                             "--plan must not write the agent")
            self.assertIn("plan:", res.stdout)

    def test_renders_a_valid_plist_naming_the_reclaimer(self):
        # Render through the same path the installer uses, so a template that
        # stops invoking `tartci reclaim` fails here rather than on a host.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "rendered.plist"
            res = run(["python3", "scripts/render_launchd_template.py",
                       f"launchd/{LABEL}.plist.template", "--set", f"HOME={td}"])
            self.assertEqual(res.returncode, 0, res.stderr)
            out.write_text(res.stdout)
            spec = plistlib.loads(out.read_bytes())
            self.assertEqual(spec["Label"], LABEL)
            self.assertIn("reclaim", spec["ProgramArguments"])
            self.assertIn("--fix", spec["ProgramArguments"])
            # Space-triggered, not calendar-triggered: the whole point is to act
            # when the volume is filling, and an hourly pass is what makes the
            # pressure thresholds meaningful.
            self.assertGreater(int(spec["StartInterval"]), 0)
            env = spec.get("EnvironmentVariables") or {}
            self.assertIn("TARTCI_RECLAIM_PRESSURE_FREE_GB", env)
            self.assertIn("TARTCI_RECLAIM_FAIL_BELOW_GB", env)

    def test_rejects_an_unknown_argument(self):
        res = run([str(INSTALLER), "--wat"])
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)


class NeverTouchesTheRealDomainFromATempHome(unittest.TestCase):
    """The leak: a temp-HOME install replaced the host's real reclaim agent.

    `tartci setup` run by tests/test_tart_channel.sh with HOME=$tmp/home
    reached this installer, which bootstrapped $tmp/home's plist into the one
    real gui/<uid> domain after booting the real job out. The real launchctl is
    now reachable only from the account's own home and LaunchAgents dir.
    """

    def real_registration(self) -> str | None:
        probe = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                               capture_output=True, text=True, check=False) \
            if Path("/bin/launchctl").exists() else None
        if probe is None or probe.returncode != 0:
            return None
        return next((line.strip() for line in probe.stdout.splitlines()
                     if line.strip().startswith("path = ")), None)

    def test_temp_home_with_the_real_launchctl_is_refused_before_any_write(self):
        before = self.real_registration()
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            res = run([str(INSTALLER), "--install"], {"HOME": str(home)})
            self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
            self.assertIn("is not this account's home", res.stderr)
            self.assertFalse((home / "Library" / "LaunchAgents" / f"{LABEL}.plist").exists())
        self.assertEqual(self.real_registration(), before,
                         "the real domain's reclaim registration changed")

    def test_real_home_but_foreign_agents_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            res = run([str(INSTALLER), "--install"],
                      {"TARTCI_AGENTS_DIR": str(Path(td) / "agents")})
            self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
            self.assertIn("is not under", res.stderr)

    def test_a_test_double_installs_into_the_temp_home(self):
        # Control: the guard refuses the real domain, not installation. With a
        # double the same temp-HOME install succeeds and bootstraps ITS plist.
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            double, calls = launchctl_double(Path(td))
            res = run([str(INSTALLER), "--install"],
                      {"HOME": str(home), "TARTCI_LAUNCHCTL_BIN": str(double)})
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            plist = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            self.assertTrue(plist.is_file())
            self.assertIn(f"bootstrap gui/{os.getuid()} {plist}", calls.read_text())

    def test_self_update_installer_carries_the_same_guard(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            res = run([str(ROOT / "scripts" / "install_self_update_agent.sh"), "--install"],
                      {"HOME": str(home), "TARTCI_SELF_UPDATE_SKIP_PEERS": "1"})
            self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
            self.assertIn("is not this account's home", res.stderr)

    def test_shell_suites_that_run_setup_cannot_reach_the_real_domain(self):
        # tests/test_tart_channel.sh runs `tartci setup` with a temp HOME. It
        # must now see the installer refuse, not silently register.
        body = (ROOT / "tests" / "test_tart_channel.sh").read_text()
        self.assertIn("launchd guard: HOME=", body)


if __name__ == "__main__":
    unittest.main()
