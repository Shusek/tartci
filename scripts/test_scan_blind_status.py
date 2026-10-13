import tempfile
import unittest
from pathlib import Path

from scan_blind_status import blind_problems


class ScanBlindStatusTests(unittest.TestCase):
    def test_retired_lane_log_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            active = root / "macos-fleet-pulp-gate.log"
            retired = root / "macos-fleet-old-gate.log"
            active.write_text("SCAN BLIND 1/9\nSCAN BLIND 2/9\nSCAN BLIND 3/9\n")
            retired.write_text("SCAN BLIND 1/9\nSCAN BLIND 2/9\nSCAN BLIND 3/9\n")
            receipt = {"plists": {
                "com.danielraffel.tartci.tart-runner-macos-fleet.m3.pulp-gate.plist": "sha",
            }}
            problems = blind_problems(receipt, [str(active), str(retired)], 3)
            self.assertEqual([str(active)], [row["label"] for row in problems])

    def test_active_lane_streak_is_reported(self):
        receipt = {"plists": {"pulp-gate.plist": "sha"}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "macos-fleet-pulp-gate.log"
            path.write_text("SCAN BLIND 1/9\nSCAN BLIND 2/9\n")
            self.assertEqual(
                [{"code": "scan_blind", "label": str(path), "cycles": 2}],
                blind_problems(receipt, [str(path)], 2),
            )


if __name__ == "__main__":
    unittest.main()
