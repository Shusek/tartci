#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "storage_health.py"


class StorageHealthTests(unittest.TestCase):
    def run_probe(self, path: Path, state: Path, **extra):
        args = [sys.executable, str(SCRIPT), "--path", str(path), "--state", str(state)]
        for key, value in extra.items():
            args.extend([f"--{key.replace('_', '-')}", str(value)])
        return subprocess.run(args, text=True, capture_output=True, check=False, timeout=10)

    def test_metadata_probe_is_healthy_without_creating_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            result = self.run_probe(root, state)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["reason"], "directory_metadata_ok")
            self.assertEqual(list(root.iterdir()), [])

    def test_missing_path_is_not_created(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "missing-volume"
            state = Path(temp) / "state.json"
            result = self.run_probe(root, state)
            self.assertEqual(result.returncode, 10, result.stderr)
            self.assertFalse(root.exists())

    def test_two_failures_quarantine_and_three_passes_recover(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            root.rmdir()
            first = self.run_probe(root, state, failure_threshold=2, recovery_threshold=3)
            second = self.run_probe(root, state, failure_threshold=2, recovery_threshold=3)
            root.mkdir()
            self.assertEqual(first.returncode, 10)
            self.assertEqual(second.returncode, 20)
            for index in (1, 2):
                recovering = self.run_probe(root, state, failure_threshold=2, recovery_threshold=3)
                self.assertEqual(recovering.returncode, 20, index)
            recovered = self.run_probe(root, state, failure_threshold=2, recovery_threshold=3)
            self.assertEqual(recovered.returncode, 0)
            self.assertEqual(json.loads(recovered.stdout)["status"], "healthy")

    def test_quarantine_marker_survives_state_loss(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            root.rmdir()
            self.assertEqual(self.run_probe(root, state).returncode, 10)
            self.assertEqual(self.run_probe(root, state).returncode, 20)
            root.mkdir()
            self.assertEqual(self.run_probe(root, state).returncode, 20)
            state.unlink()
            still_closed = self.run_probe(root, state)
            self.assertEqual(still_closed.returncode, 20)

    def test_unbounded_configuration_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            result = self.run_probe(root, state, timeout="61")
            self.assertEqual(result.returncode, 20)

    def test_probe_timeout_is_bounded_and_degraded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            result = self.run_probe(root, state, timeout="0.001")
            self.assertEqual(result.returncode, 10)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["reason"], "probe_timeout")
            self.assertLessEqual(payload["elapsed_seconds"], 0.001)

    def test_runner_guards_once_before_pool_admission(self):
        runner = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        once = runner.rsplit("else\n", 1)[1]
        self.assertLess(once.index("tartci_storage_health_check"), once.index("tartci_pool_admission_open"))
        self.assertLess(once.index("tartci_claim_macos_slot"), once.index("run_one 1"))
        self.assertIn("running_macos_vms", once)
        self.assertIn('[ -z "$CURRENT_VM" ] || continue', runner)

    def test_loop_guards_storage_before_warm_reconciliation(self):
        runner = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        loop = runner[runner.index("heartbeat loop\n  while true; do"):]
        self.assertLess(loop.index("tartci_storage_health_check"), loop.index("tartci_warm_tick"))
        self.assertIn("heartbeat yielded-storage-health", runner)

    def test_runner_has_one_storage_gate_and_uses_read_only_probe(self):
        """The #625 runner gate must not be shadowed by a sourced duplicate."""
        runner = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        self.assertEqual(runner.count("tartci_storage_health_check(){"), 1)
        self.assertNotIn("providers/common/storage-health.lib.sh", runner)
        body = runner[runner.index("tartci_storage_health_check(){"):]
        body = body[:body.index("\nruntime_emit_complete(){")]
        self.assertIn("scripts/storage_health.py", body)
        self.assertIn("--failure-threshold", body)
        self.assertIn("--recovery-threshold", body)
        probe = (ROOT / "scripts" / "storage_health.py").read_text()
        self.assertIn("O_RDONLY", probe)
        self.assertIn("O_DIRECTORY", probe)
        self.assertIn("os.fstat", probe)
        self.assertNotIn("mkstemp(prefix=\".tartci-storage-probe-\"", probe)
        self.assertNotIn("write_fsync_unlink_ok", probe)

    def test_storage_gate_does_not_change_capacity_or_active_vm_state(self):
        runner = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        body = runner[runner.index("tartci_storage_health_check(){"):]
        body = body[:body.index("\nruntime_emit_complete(){")]
        self.assertNotIn("tartci_vm_lease", body)
        self.assertNotIn("tartci_effective_cap", body)
        self.assertNotIn("tart stop", body)
        self.assertNotIn("tart delete", body)

    def test_unreadable_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "volume"
            root.mkdir()
            state = Path(temp) / "state.json"
            state.write_text("not json")
            result = self.run_probe(root, state)
            self.assertEqual(result.returncode, 20)
            self.assertEqual(json.loads(result.stdout)["reason"], "health_state_unreadable")


if __name__ == "__main__":
    unittest.main()
