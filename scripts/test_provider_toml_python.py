#!/usr/bin/env python3
"""Provider scripts run their tomllib helpers under a Python that has it.

An operator runs the golden provisioners directly over ssh. On m1 the login
shell's `python3` is /usr/bin/python3 3.9.6, which has no tomllib, so
`pulp-source-pin.py` and `pulp-macos-readiness.py`, run with a bare `python3`,
died with `ModuleNotFoundError: No module named 'tomllib'`. They now go through
`tartci_toml_python` from providers/common/toml-python.lib.sh, the resolver the
tartci shim uses.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "providers" / "common"


def toml_helpers() -> list[str]:
    return sorted(path.name for path in COMMON.glob("*.py")
                  if re.search(r"^import tomllib\b", path.read_text(), re.M))


def provider_scripts() -> list[Path]:
    return sorted((ROOT / "providers").rglob("*.sh"))


def bare_calls(text: str, helper: str) -> list[int]:
    """Lines where `text` runs `helper` with python3 itself, bare or by path, not the resolver."""
    names = re.findall(r'^\s*([A-Z_]+)="[^"\n]*/' + re.escape(helper) + '"', text, re.M)
    lines = []
    for target in [re.escape(helper)] + [r"\$\{?" + name + r"\}?" for name in names]:
        for match in re.finditer(r'(?<![\w.-])python3 "?[^"\s]*' + target, text):
            lines.append(text.count("\n", 0, match.start()) + 1)
    return sorted(lines)


class StaticTests(unittest.TestCase):
    def test_no_provider_script_runs_a_toml_helper_with_a_bare_python3(self) -> None:
        helpers = toml_helpers()
        # Control: the two helpers this guards are found.
        self.assertIn("pulp-source-pin.py", helpers)
        self.assertIn("pulp-macos-readiness.py", helpers)
        bare = [f"{script.relative_to(ROOT)}:{line}: {helper}"
                for script in provider_scripts() for helper in helpers
                for line in bare_calls(script.read_text(), helper)]
        self.assertEqual(bare, [])

    def test_the_scan_sees_a_bare_call_by_variable_and_by_path(self) -> None:
        text = ('PIN="$ROOT/providers/common/pulp-source-pin.py"\n'
                'x="$(python3 "$PIN" "$M")"\n'
                'python3 "$ROOT/providers/common/pulp-source-pin.py" m\n'
                'tartci_toml_python "$PIN" "$M"\n'
                '/usr/bin/python3 "${PIN}" m\n')
        self.assertEqual(bare_calls(text, "pulp-source-pin.py"), [2, 3, 5])


class OperatorShellTests(unittest.TestCase):
    """`provision.sh pulp-readiness` under a PATH whose python3 lacks tomllib."""

    def run_readiness(self, toml_python: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as td:
            bindir = Path(td) / "bin"
            bindir.mkdir()
            fake = bindir / "python3"
            # A 3.9 stand-in: no tomllib, and it says so if it is ever run.
            fake.write_text("#!/bin/sh\n"
                            "case \"$*\" in *'import tomllib'*) exit 1;; esac\n"
                            "echo 'FAKE-PY39: ModuleNotFoundError: tomllib' >&2; exit 1\n")
            fake.chmod(0o755)
            for versioned in ("python3.11", "python3.12"):
                stub = bindir / versioned
                stub.write_text("#!/bin/sh\nexit 1\n")
                stub.chmod(0o755)
            env = {**os.environ, "PATH": f"{bindir}:/bin:/usr/bin",
                   "TARTCI_PYTHON": toml_python, "HOME": td}
            return subprocess.run(
                ["bash", str(ROOT / "providers" / "tart-macos" / "provision.sh"),
                 "pulp-readiness"], env=env, capture_output=True, text=True, timeout=60)

    def test_readiness_runs_under_the_resolved_toml_python(self) -> None:
        if sys.version_info < (3, 11):
            self.skipTest("needs a Python 3.11+ to resolve to")
        result = self.run_readiness(sys.executable)
        self.assertNotIn("FAKE-PY39", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        # The reporter read the TOML manifest: its report names the pinned source.
        self.assertIn('"pulp_commit"', result.stdout)
        self.assertIn('"status": "unready"', result.stdout)

    def test_no_toml_python_names_the_fix(self) -> None:
        result = self.run_readiness("")
        if Path("/opt/homebrew/bin/python3").exists() or Path("/usr/local/bin/python3").exists():
            # The resolver also tries the Homebrew paths, which a dev Mac has.
            self.skipTest("a Homebrew python3 on this machine would be resolved")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("FAKE-PY39", result.stderr)
        self.assertIn("set TARTCI_PYTHON", result.stderr)


if __name__ == "__main__":
    unittest.main()
