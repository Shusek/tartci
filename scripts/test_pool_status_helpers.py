#!/usr/bin/env python3
"""Guards for the read-only helpers used by pool status."""
from __future__ import annotations

import os
import re
import resource
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def bounded_nproc() -> None:
    """Keep recursion tests bounded without setting a limit below this host's load."""
    current = len(subprocess.check_output(
        ["ps", "-u", str(os.getuid()), "-o", "pid="]
    ).splitlines())
    resource.setrlimit(resource.RLIMIT_NPROC, (current + 300, current + 300))
HELPERS = (
    "warm_vm_status.py",
    "reclaim_status.py",
    "power_status.py",
    "launchd_interval_guard.py",
    "lease_fit.py",
    "signing_prompt_guard.py",
    "ccache_guard.py",
    "launchctl_guard.py",
    "status.py",
    "governor.py",
    "http_connect_ssh_relay.py",
    "reuse_canary.py",
    "pool_usage.py",
)


class PoolStatusHelperTests(unittest.TestCase):
    def test_helper_comments_have_no_shell_command_backticks(self) -> None:
        pattern = re.compile(r"`[^`]*\s[^`]*`")
        self.assertIsNotNone(pattern.search("module docs: " + "`tartci pool status`"))
        for name in HELPERS:
            text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertIsNone(pattern.search(text), name)

    def test_failed_verified_interpreter_names_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tartci-bad-python-") as tmp:
            fake = Path(tmp) / "python3"
            fake.write_text("""#!/bin/sh
case "$*" in
  *'import sys'*) exit 1 ;;
  *) exit 0 ;;
esac
""")
            fake.chmod(0o755)
            env = os.environ.copy()
            env.update(HOME=tmp, PATH=f"{tmp}:/usr/bin:/bin", TARTCI_PYTHON=str(fake))
            result = subprocess.run(
                ["bash", "-c", f"source {ROOT / 'tartci'}; tartci_verified_python"],
                cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 127)
            self.assertIn(str(fake), result.stderr)

    def test_pool_status_refuses_unverified_helper(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tartci-bad-status-python-") as tmp:
            fake = Path(tmp) / "python3"
            fake.write_text("""#!/bin/sh
case "$*" in
  *'import sys'*) exit 1 ;;
  *) exit 0 ;;
esac
""")
            fake.chmod(0o755)
            env = os.environ.copy()
            env.update(HOME=tmp, PATH=f"{tmp}:/usr/bin:/bin", TARTCI_PYTHON=str(fake))
            result = subprocess.run(
                ["bash", str(ROOT / "tartci"), "pool", "status", "--json"],
                cwd=ROOT, env=env, text=True, capture_output=True,
                timeout=5, check=False,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn(str(fake), result.stderr)

    def test_shell_named_python3_cannot_recurse_pool_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tartci-shell-python-") as tmp:
            root = Path(tmp)
            fake = root / "python3"
            fake.write_text("""#!/bin/sh
            if [ "$1" = "-c" ]; then
  case "$2" in
    *'sys.implementation.name'*) printf 'not-python\\n'; exit 0 ;;
    *) exit 0 ;;
  esac
fi
exec /bin/sh "$@"
""")
            fake.chmod(0o755)
            counter = root / "count"
            fake_tartci = root / "tartci"
            fake_tartci.write_text(f"#!/bin/sh\nprintf x >> {counter}\n")
            fake_tartci.chmod(0o755)
            env = os.environ.copy()
            env.update(
                HOME=tmp,
                PATH=f"{tmp}:/usr/bin:/bin",
                TARTCI_PYTHON=str(fake),
            )
            for argv in (("pool", "status", "--json"), ("pool", "status", "--usage"),
                         ("pool", "status"), ("doctor", "--json")):
                with self.subTest(argv=argv):
                    result = subprocess.run(
                        ["bash", str(ROOT / "tartci"), *argv], cwd=ROOT, env=env,
                        text=True, capture_output=True, timeout=5, check=False,
                        preexec_fn=bounded_nproc,
                    )
                    self.assertNotEqual(result.returncode, 124)
                    self.assertNotIn("recursion", result.stderr.lower())
            self.assertFalse(counter.exists())

    def test_python_shim_is_not_exported_to_children(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tartci-env-") as tmp:
            env = dict(os.environ, HOME=tmp, PATH=f"{tmp}:/usr/bin:/bin",
                       TARTCI_PYTHON="/usr/bin/python3")
            result = subprocess.run(
                ["bash", "-c", f"source {ROOT / 'tartci'}; env"],
                cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0)
            self.assertNotRegex(result.stdout, r"BASH_FUNC_[^=]+=\(\)")


if __name__ == "__main__":
    unittest.main()
