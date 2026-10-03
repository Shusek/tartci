"""Execute provider scope resolution with no golden, VM, or credentials."""
from __future__ import annotations

import os
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RunnerScopeTests(unittest.TestCase):
    def resolve(self, provider: str, scope: str | None, group: str = "1") -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as directory:
            env = {"PATH": os.environ["PATH"], "HOME": directory, "TARTCI_RUNNER_GROUP_ID": group}
            if scope is not None:
                env["TARTCI_RUNNER_SCOPE"] = scope
            return subprocess.run(
                ["/bin/bash", str(ROOT / "providers" / provider / "runner.sh"), "--repo", "SuvioMedia/Suvio", "--print-runner-api-root"],
                env=env, capture_output=True, text=True, timeout=5,
            )

    def test_explicit_organization_scope_supports_default_group(self) -> None:
        for provider in ("tart-macos", "qemu-windows"):
            with self.subTest(provider=provider):
                result = self.resolve(provider, "org")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "orgs/SuvioMedia/actions/runners")

    def test_explicit_repository_scope_is_independent_of_group(self) -> None:
        for provider in ("tart-macos", "qemu-windows"):
            result = self.resolve(provider, "repo", "3")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "repos/SuvioMedia/Suvio/actions/runners")

    def test_existing_defaults_are_preserved(self) -> None:
        for provider, group, expected in (
            ("tart-macos", "1", "repos/SuvioMedia/Suvio/actions/runners"),
            ("tart-macos", "3", "orgs/SuvioMedia/actions/runners"),
            ("qemu-windows", "3", "repos/SuvioMedia/Suvio/actions/runners"),
        ):
            result = self.resolve(provider, None, group)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)

    def test_invalid_scope_and_group_fail(self) -> None:
        for provider in ("tart-macos", "qemu-windows"):
            for scope, group in (("organization", "1"), ("org", "0"), ("org", "01")):
                with self.subTest(provider=provider, scope=scope, group=group):
                    self.assertNotEqual(self.resolve(provider, scope, group).returncode, 0)

    def test_windows_create_list_delete_use_same_organization_scope(self) -> None:
        body = (ROOT / "providers/qemu-windows/runner.sh").read_text()
        delete = body[body.index("delete_runner_registration(){"):body.index("\ncleanup_active_windows_job(){")]
        start = body.index('  jit="$("$GH_CLI" api -X POST')
        end = body.index(')" || {', start) + len(')"')
        mint = body[start:end]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gh = root / "fake-gh"
            gh.write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["CALLS"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if "POST" in sys.argv:
    print("fixture-jit")
elif any(".busy==false" in arg for arg in sys.argv):
    print("41")
''')
            gh.chmod(0o700)
            script = 'set -euo pipefail\n' + delete + '\n' + mint + '\ndelete_runner_registration "$job"\n'
            env = {
                "PATH": os.environ["PATH"], "HOME": directory,
                "GH_CLI": str(gh), "RUNNER_API_ROOT": "orgs/SuvioMedia/actions/runners",
                "RUNNER_GROUP_ID": "1", "job": "fixture-runner", "CALLS": str(root / "calls.jsonl"),
            }
            result = subprocess.run(
                ["/bin/bash", "-c", 'note(){ :; }; label_args=(labels[]=fixture); ' + script],
                env=env, text=True, capture_output=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
            self.assertEqual([call[call.index("api") + 1] for call in calls if "-X" not in call], ["orgs/SuvioMedia/actions/runners"] * 2)
            mutation_paths = [call[call.index("-X") + 2] for call in calls if "-X" in call]
            self.assertEqual(mutation_paths, ["orgs/SuvioMedia/actions/runners/generate-jitconfig", "orgs/SuvioMedia/actions/runners/41"])


if __name__ == "__main__":
    unittest.main()
