import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CpuPressureEntryPointTests(unittest.TestCase):
    def run_guard(self, profile: str, *, kill: bool = False):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ps = root / "ps"
            ps.write_text("#!/bin/sh\ncat <<'EOF'\n123 1 01:00 00:20 /Users/test/agent-worktrees/runaway/python\n124 1 01:00 00:20 /usr/bin/tart vm run\nEOF\n")
            ps.chmod(ps.stat().st_mode | stat.S_IXUSR)
            config = root / "profile.toml"
            config.write_text(profile)
            env = {**os.environ, "TARTCI_CPU_GUARD_PS": str(ps)}
            args = [str(ROOT / "tartci"), "doctor", "cpu-pressure", "--profile",
                    str(config), "--json"]
            if kill:
                args.append("--kill")
            return subprocess.run(args, env=env, text=True, capture_output=True)

    def test_alert_attributes_process_tree_and_worktree(self):
        result = self.run_guard("[cpu_pressure]\nthreshold_cpu_seconds = 10\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual([row["pid"] for row in value["offenders"]], [123])
        row = value["offenders"][0]
        self.assertEqual(row["action"], "alert")
        self.assertIn("agent-worktrees/runaway", row["worktree"])
        self.assertIn("parent_session", row)
        self.assertIn("process_tree", row)

    def test_profile_must_opt_in_before_kill_and_allowlist_is_excluded(self):
        result = self.run_guard("[cpu_pressure]\nthreshold_cpu_seconds = 10\n", kill=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["offenders"][0]["action"], "alert")
        self.assertEqual(value["kill_enabled"], False)
        self.assertNotIn(124, [row["pid"] for row in value["offenders"]])


if __name__ == "__main__":
    unittest.main()
