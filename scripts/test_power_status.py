"""power_status reads the configured AC profile, not live assertions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import power_status  # noqa: E402

STUDIO = """\
System-wide power settings:
Currently in use:
AC Power:
 Sleep On Power Button 1
 displaysleep         10
 sleep                0
 autorestart          1
"""

LAPTOP = """\
Battery Power:
 sleep                1
 displaysleep         2
AC Power:
 sleep                10
 displaysleep         10
"""


class PowerStatusTests(unittest.TestCase):
    def test_never_sleeping_studio_is_ok(self) -> None:
        value = power_status.status(STUDIO)
        self.assertEqual(value["state"], "ok")
        self.assertEqual(value["autorestart"], 1)
        self.assertEqual(power_status.describe(value), "power: ok (never sleeps on AC)")

    def test_ac_sleep_warns_and_ignores_the_battery_section(self) -> None:
        value = power_status.status(LAPTOP)
        self.assertEqual(value, {"state": "sleeps", "sleep_minutes": 10, "autorestart": None})
        self.assertIn("power: WARN sleeps after 10 min", power_status.describe(value))
        self.assertIn("Prevent automatic sleeping", power_status.describe(value))

    def test_multiword_keys_parse(self) -> None:
        self.assertEqual(power_status.parse_custom(STUDIO)["Sleep On Power Button"], 1)

    def test_no_ac_profile_is_unknown_not_ok(self) -> None:
        for text in ("", "Battery Power:\n sleep 0\n", "pmset: command not found\n"):
            with self.subTest(text=text):
                self.assertEqual(power_status.status(text)["state"], "unknown")


if __name__ == "__main__":
    unittest.main()


class FleetDoctorPowerTests(unittest.TestCase):
    def test_doctor_flags_a_sleeping_host_and_explains_it(self) -> None:
        import json
        import fleet_doctor

        sleeping = fleet_doctor.check_power(power_status.status(LAPTOP))
        self.assertEqual((sleeping.state, sleeping.code), (fleet_doctor.PROBLEM, "power_sleeps"))
        awake = fleet_doctor.check_power(power_status.status(STUDIO))
        self.assertEqual((awake.state, awake.code), (fleet_doctor.OK, "power_ok"))
        unknown = fleet_doctor.check_power(power_status.status(""))
        self.assertEqual(unknown.state, fleet_doctor.UNKNOWN)
        reasons = json.loads((Path(__file__).resolve().parent / "fleet_reasons.json").read_text())
        for code in ("power_ok", "power_sleeps", "power_unknown"):
            self.assertIn(code, fleet_doctor.CODES)
            self.assertIn(code, json.dumps(reasons))
        self.assertIn("Prevent automatic sleeping", json.dumps(reasons))
