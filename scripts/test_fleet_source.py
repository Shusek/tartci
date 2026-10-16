#!/usr/bin/env python3
import os
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_source


class FleetSourceTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.pop("TARTCI_FLEET_SOURCE_ROOT", None)
        self._old_commit = os.environ.pop("TARTCI_FLEET_SOURCE_COMMIT", None)
        self._old_repository = os.environ.pop("TARTCI_FLEET_SOURCE_REPOSITORY", None)

    def tearDown(self):
        if self._old is not None:
            os.environ["TARTCI_FLEET_SOURCE_ROOT"] = self._old
        else:
            os.environ.pop("TARTCI_FLEET_SOURCE_ROOT", None)
        if self._old_commit is not None:
            os.environ["TARTCI_FLEET_SOURCE_COMMIT"] = self._old_commit
        else:
            os.environ.pop("TARTCI_FLEET_SOURCE_COMMIT", None)
        if self._old_repository is not None:
            os.environ["TARTCI_FLEET_SOURCE_REPOSITORY"] = self._old_repository
        else:
            os.environ.pop("TARTCI_FLEET_SOURCE_REPOSITORY", None)

    def test_default_uses_checkout_without_validation_override(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = fleet_source.resolve(root)
            self.assertEqual(root.resolve(), source.root)
            self.assertFalse(source.configured)

    def test_explicit_source_requires_profiles_and_published_supply(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            os.environ["TARTCI_FLEET_SOURCE_ROOT"] = str(root)
            with self.assertRaisesRegex(ValueError, "profiles directory"):
                fleet_source.resolve(Path("/public"))

    def test_explicit_source_is_validated_and_selected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "profiles").mkdir()
            (root / "profiles" / "canary-macos-fleet.toml").write_text("schema = 1\n")
            (root / "fleet").mkdir()
            (root / "fleet" / "advertised-labels.json").write_text("{}\n")
            os.environ["TARTCI_FLEET_SOURCE_ROOT"] = str(root)
            os.environ["TARTCI_FLEET_SOURCE_COMMIT"] = "0123456789abcdef0123456789abcdef01234567"
            os.environ["TARTCI_FLEET_SOURCE_REPOSITORY"] = "danielraffel/pulp-planning"
            source = fleet_source.resolve(Path("/public"))
            self.assertTrue(source.configured)
            self.assertEqual(root.resolve(), source.root)
            self.assertEqual(root.resolve() / "profiles", source.profiles_dir)
            self.assertEqual(
                "0123456789abcdef0123456789abcdef01234567", source.source_commit)
            self.assertEqual("danielraffel/pulp-planning", source.repository)


if __name__ == "__main__":
    unittest.main()
