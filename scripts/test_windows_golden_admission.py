"""Fail-closed Windows golden receipt and fast-cache controls."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


VERIFY = Path(__file__).with_name("verify_windows_golden.py")
SNAPSHOT = VERIFY.parent.parent / "providers/qemu-windows/prepare-golden-snapshot.sh"
# The snapshot script hashes with the pinned interpreter, which must be 3.11+
# (tomllib); the test hands it this one.
NEEDS_PINNABLE = unittest.skipIf(sys.version_info < (3, 11),
                                 "the snapshot interpreter must be 3.11+ (tomllib)")


class WindowsGoldenAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "base.qcow2"
        self.cache = self.root / "cache"
        self.image.write_bytes(b"small qcow2 fixture bytes")

    def sidecar(self, digest=None):
        if digest is None:
            digest = hashlib.sha256(self.image.read_bytes()).hexdigest()
        Path(f"{self.image}.sha256").write_text(f"{digest}  /old/location/base.qcow2\n")

    def run_verify(self):
        return subprocess.run(
            [sys.executable, str(VERIFY), str(self.image), "--cache-dir", str(self.cache)],
            capture_output=True, text=True, check=False,
        )

    def test_missing_and_mismatched_sidecar_refuse(self):
        self.assertNotEqual(self.run_verify().returncode, 0)
        self.sidecar("0" * 64)
        failed = self.run_verify()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("SHA-256 mismatch", failed.stderr)
        self.assertEqual(list(self.cache.glob("*.json")), [])

    def test_verified_image_reuses_cache_until_bytes_change(self):
        self.sidecar()
        first = self.run_verify()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("(hashed)", first.stdout)
        second = self.run_verify()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("(cached)", second.stdout)

        original_mtime = self.image.stat().st_mtime_ns
        time.sleep(0.02)
        self.image.write_bytes(b"changed qcow2 fixture byt")  # same byte count
        os.utime(self.image, ns=(original_mtime, original_mtime))
        changed = self.run_verify()
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("SHA-256 mismatch", changed.stderr)

    def test_sidecar_change_invalidates_cache(self):
        self.sidecar()
        self.assertEqual(self.run_verify().returncode, 0)
        self.sidecar("f" * 64)
        changed = self.run_verify()
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("SHA-256 mismatch", changed.stderr)

    def test_symlink_image_refuses(self):
        target = self.root / "target.qcow2"
        self.image.rename(target)
        self.image.symlink_to(target)
        self.sidecar(hashlib.sha256(target.read_bytes()).hexdigest())
        failed = self.run_verify()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("symlink", failed.stderr)

    def test_cache_record_symlink_is_not_trusted(self):
        self.sidecar()
        self.assertEqual(self.run_verify().returncode, 0)
        record = next(self.cache.glob("*.json"))
        outside = self.root / "outside.json"
        record.rename(outside)
        record.symlink_to(outside)
        result = self.run_verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(hashed)", result.stdout)
        self.assertFalse(record.is_symlink())

    def shadowed_path(self) -> str:
        # A python3 first on PATH that fails loudly and leaves a trace: the
        # golden must never be hashed by whatever PATH resolves to.
        shadow = self.root / "shadow-bin"
        shadow.mkdir(exist_ok=True)
        self.shadow_trace = self.root / "shadow-python3-ran"
        for name in ("python3", "python3.12", "python3.11"):
            stub = shadow / name
            stub.write_text(f"#!/bin/sh\ntouch {self.shadow_trace}\nexit 97\n")
            stub.chmod(0o755)
        return f"{shadow}:/usr/bin:/bin"

    def snapshot_env(self, **extra):
        home = self.root / "home"
        home.mkdir(exist_ok=True)
        env = {"HOME": str(home), "PATH": self.shadowed_path(),
               "TARTCI_PYTHON": sys.executable}
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    @NEEDS_PINNABLE
    @unittest.skipUnless(sys.platform == "darwin", "APFS CoW snapshot needs macOS")
    def test_the_snapshot_is_sealed_hashed_by_the_pinned_interpreter_and_timed(self):
        import stat  # noqa: PLC0415
        self.sidecar()
        snapshot = self.root / "snapshot.qcow2"
        result = subprocess.run([str(SNAPSHOT), str(self.image), str(snapshot)],
                                capture_output=True, text=True, check=False,
                                env=self.snapshot_env())
        self.addCleanup(lambda: subprocess.run(
            ["chflags", "nouchg", str(snapshot), f"{snapshot}.sha256"],
            capture_output=True, check=False))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.shadow_trace.exists(), "PATH's python3 hashed the golden")
        for path in (snapshot, Path(f"{snapshot}.sha256")):
            with self.subTest(path=path.name):
                self.assertTrue(path.stat().st_flags & stat.UF_IMMUTABLE)
        self.assertRegex(result.stderr, r"Windows snapshot hash: \d+s for \d+ bytes")
        self.assertIn(sys.executable, result.stderr)

    def test_without_a_pin_under_launchd_the_snapshot_is_refused_not_hashed_by_path(self):
        self.sidecar()
        snapshot = self.root / "snapshot.qcow2"
        result = subprocess.run([str(SNAPSHOT), str(self.image), str(snapshot)],
                                capture_output=True, text=True, check=False,
                                env=self.snapshot_env(TARTCI_PYTHON=None, TARTCI_LAUNCHD="1"))
        self.assertEqual(result.returncode, 127, result.stderr)
        self.assertIn("refusing an unpinned interpreter", result.stderr)
        self.assertFalse(self.shadow_trace.exists())
        self.assertFalse(snapshot.exists())

    def test_the_windows_providers_never_hash_with_path_python3(self):
        providers = VERIFY.parent.parent / "providers/qemu-windows"
        for name in ("runner.sh", "run.sh", "prepare-golden-snapshot.sh"):
            with self.subTest(name=name):
                text = (providers / name).read_text()
                self.assertIn("verify_windows_golden.py", text)
                for line in text.splitlines():
                    if "verify_windows_golden.py" in line:
                        self.assertNotRegex(line, r"(^|\s)python3\s")
                self.assertIn("tartci_toml_python_path", text)

    @NEEDS_PINNABLE
    @unittest.skipUnless(sys.platform == "darwin", "APFS CoW snapshot needs macOS")
    def test_private_snapshot_survives_source_swap_and_refuses_bad_receipt(self):
        self.sidecar()
        snapshot = self.root / "snapshot.qcow2"
        original = self.image.read_bytes()
        result = subprocess.run([str(SNAPSHOT), str(self.image), str(snapshot)],
                                capture_output=True, text=True, check=False,
                                env=self.snapshot_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: subprocess.run(
            ["chflags", "nouchg", str(snapshot), f"{snapshot}.sha256"],
            capture_output=True, check=False))
        self.image.write_bytes(b"mutated source after verified snapshot")
        self.assertEqual(snapshot.read_bytes(), original)
        checked = subprocess.run(
            [sys.executable, str(VERIFY), "--no-cache", str(snapshot)],
            capture_output=True, text=True, check=False)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        bad = self.root / "bad.qcow2"
        bad.write_bytes(b"other bytes")
        Path(f"{bad}.sha256").write_text("0" * 64 + "  old.qcow2\n")
        refused = subprocess.run([str(SNAPSHOT), str(bad), str(self.root / "refused.qcow2")],
                                 capture_output=True, text=True, check=False,
                                 env=self.snapshot_env())
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.root / "refused.qcow2").exists())


if __name__ == "__main__":
    unittest.main()
