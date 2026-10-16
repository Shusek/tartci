#!/usr/bin/env python3
import os
import json
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
            profile_digest = fleet_source.FleetSource(
                root, root / "profiles", root / "fleet" / "advertised-labels.json", True
            ).profiles_sha256
            (root / "fleet" / "advertised-labels.json").write_text(json.dumps({
                "schema": "tartci.advertised-labels/v1",
                "generated_from": {
                    "repo": "ORG/private-fleet-authority",
                    "commit": "0123456789abcdef0123456789abcdef01234567",
                    "profiles": ["profiles/canary-macos-fleet.toml"],
                    "profiles_sha256": profile_digest,
                },
                "registrations": [{}],
            }) + "\n")
            os.environ["TARTCI_FLEET_SOURCE_ROOT"] = str(root)
            os.environ["TARTCI_FLEET_SOURCE_COMMIT"] = "0123456789abcdef0123456789abcdef01234567"
            os.environ["TARTCI_FLEET_SOURCE_REPOSITORY"] = "ORG/private-fleet-authority"
            source = fleet_source.resolve(Path("/public"))
            self.assertTrue(source.configured)
            self.assertEqual(root.resolve(), source.root)
            self.assertEqual(root.resolve() / "profiles", source.profiles_dir)
            self.assertEqual(
                "0123456789abcdef0123456789abcdef01234567", source.source_commit)
            self.assertEqual("ORG/private-fleet-authority", source.repository)
            self.assertRegex(source.tree_sha256, r"^[0-9a-f]{64}$")
            self.assertRegex(source.published_supply_sha256, r"^[0-9a-f]{64}$")

    def test_explicit_source_rejects_repository_or_commit_provenance_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "profiles").mkdir()
            (root / "profiles" / "canary-macos-fleet.toml").write_text("schema = 1\n")
            (root / "fleet").mkdir()
            supply = root / "fleet" / "advertised-labels.json"
            profile_digest = fleet_source.FleetSource(
                root, root / "profiles", supply, True
            ).profiles_sha256
            supply.write_text(json.dumps({
                "schema": "tartci.advertised-labels/v1",
                "generated_from": {
                    "repo": "ORG/private-fleet-authority",
                    "commit": "a" * 40,
                    "profiles": ["profiles/canary-macos-fleet.toml"],
                    "profiles_sha256": profile_digest,
                },
                "registrations": [],
            }) + "\n")
            os.environ["TARTCI_FLEET_SOURCE_ROOT"] = str(root)
            os.environ["TARTCI_FLEET_SOURCE_COMMIT"] = "b" * 40
            os.environ["TARTCI_FLEET_SOURCE_REPOSITORY"] = "ORG/private-fleet-authority"
            with self.assertRaisesRegex(ValueError, "provenance"):
                fleet_source.resolve(Path("/public"))

            os.environ["TARTCI_FLEET_SOURCE_COMMIT"] = "a" * 40
            os.environ["TARTCI_FLEET_SOURCE_REPOSITORY"] = "ORG/other-fleet-authority"
            with self.assertRaisesRegex(ValueError, "provenance"):
                fleet_source.resolve(Path("/public"))


if __name__ == "__main__":
    unittest.main()
