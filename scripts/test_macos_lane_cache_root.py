#!/usr/bin/env python3
"""runner.sh refuses to boot a lane whose own cache root is not what it names."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
LIB = ROOT / "providers" / "tart-macos" / "lane-cache-root.lib.sh"


def refusal(root: Path, host: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-c", 'source "$1"; tartci_lane_cache_root_refusal "$2" "$3"',
         "_", str(LIB), str(root), host],
        text=True, capture_output=True, check=False, env={"PATH": "/usr/bin:/bin"},
    )


class LaneCacheRootRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # realpath: macOS temp dirs sit behind the /var -> /private/var link.
        self.base = Path(os.path.realpath(tmp.name))
        self.host = self.base / "host-ci"
        (self.host / "ccache").mkdir(parents=True)
        (self.host / "ccache" / "marker").write_text("planted-by-pr-job\n", encoding="utf-8")
        (self.host / "configure-checks").mkdir()

    def test_a_real_separate_root_boots(self) -> None:
        root = self.base / "trusted"
        (root / "ccache").mkdir(parents=True)
        (root / "configure-checks").mkdir()
        result = refusal(root, str(self.host))
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_root_not_created_yet_boots(self) -> None:
        result = refusal(self.base / "trusted", str(self.host))
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_symlinked_root_is_refused(self) -> None:
        root = self.base / "trusted"
        root.symlink_to(self.host)
        # Control: through the link the "trusted" lane sees the PR-planted file.
        self.assertTrue((root / "ccache" / "marker").exists())
        result = refusal(root, str(self.host))
        self.assertEqual(result.returncode, 1)
        self.assertIn("symlink", result.stdout)

    def test_a_symlinked_mount_subdirectory_is_refused(self) -> None:
        for sub in ("ccache", "configure-checks"):
            with self.subTest(sub=sub):
                root = self.base / f"trusted-{sub}"
                root.mkdir()
                (root / sub).symlink_to(self.host / sub)
                result = refusal(root, str(self.host))
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"{root / sub} is a symlink", result.stdout)

    def test_a_root_under_a_symlinked_parent_is_refused(self) -> None:
        (self.base / "link").symlink_to(self.host)
        result = refusal(self.base / "link" / "ccache" / "..", str(self.host))
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_overlap_with_the_host_root_is_refused(self) -> None:
        inside = self.host / "trusted"
        inside.mkdir()
        cases = {
            "same": (self.host, "is or sits inside"),
            "inside": (inside, "is or sits inside"),
            "containing": (self.base, "contains the host cache root"),
        }
        for name, (root, message) in cases.items():
            with self.subTest(name=name):
                result = refusal(root, str(self.host))
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stdout)

    def test_overlap_is_judged_on_physical_paths(self) -> None:
        # The host root named through a link still overlaps the real directory.
        (self.base / "host-link").symlink_to(self.host)
        result = refusal(self.host, str(self.base / "host-link"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("is or sits inside", result.stdout)

    def test_a_host_root_lane_is_not_checked(self) -> None:
        # Lanes on the host root carry no TARTCI_HOST_CI_CACHE; nothing changes for them.
        link = self.base / "linked-host"
        link.symlink_to(self.host)
        result = refusal(link, "")
        self.assertEqual(result.returncode, 0)


class RunnerWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.body = RUNNER.read_text(encoding="utf-8")

    def test_runner_sources_the_lib_and_reads_the_host_root(self) -> None:
        self.assertIn('source "$TARTCI_ROOT/providers/tart-macos/lane-cache-root.lib.sh"', self.body)
        self.assertIn('HOST_CACHE_ROOT="${TARTCI_HOST_CI_CACHE:-}"', self.body)

    def test_refusal_runs_before_preparation_and_before_the_mounts(self) -> None:
        call = 'tartci_lane_cache_root_refusal "$CACHE_ROOT" "$HOST_CACHE_ROOT"'
        first = self.body.index(call)
        second = self.body.index(call, first + 1)
        self.assertLess(first, self.body.index('tartci_prepare_disk_root "$CACHE_ROOT/ccache"'))
        self.assertLess(first, self.body.index('tartci_ccache_guard "$CACHE_ROOT/ccache"'))
        self.assertGreater(second, self.body.index('tartci_prepare_disk_root "$CACHE_ROOT/configure-checks"'))
        self.assertLess(second, self.body.index('--dir="ccache:$CACHE_ROOT/ccache"'))
        # Each refusal stops the boot.
        for start in (first, second):
            block = self.body[start:self.body.index("\n  fi\n", start)]
            self.assertIn("return 1", block)
            self.assertIn("discard_current_vm", block)


if __name__ == "__main__":
    unittest.main()
