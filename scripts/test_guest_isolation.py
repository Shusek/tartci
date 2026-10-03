#!/usr/bin/env python3
"""Opt-in guest isolation for shared Tart lanes, and provider SSH hygiene.

A guest runs whatever job GitHub assigns its runner. These tests pin the knobs
that keep such a job from writing host caches a later job consumes, from
reaching the host or sibling VMs, and from receiving the operator's SSH agent.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "providers" / "common" / "guest-isolation.lib.sh"
MACOS = ROOT / "providers" / "tart-macos" / "runner.sh"
LINUX = ROOT / "providers" / "tart-linux" / "runner.sh"
WINDOWS = ROOT / "providers" / "qemu-windows" / "runner.sh"


def configure(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    script = (
        f"set -euo pipefail; source {str(LIB)!r}; tartci_guest_isolation_configure; "
        'printf "mode=%s suffix=%s net=%s\\n" "$TARTCI_HOST_CACHE_ACCESS_MODE" '
        '"$(tartci_host_cache_mount_suffix)" '
        '"${TARTCI_TART_NETWORK_ARGS[*]+${TARTCI_TART_NETWORK_ARGS[*]}}"'
    )
    clean = {k: v for k, v in os.environ.items() if not k.startswith("TARTCI_")}
    return subprocess.run(["bash", "-c", script], env={**clean, **env},
                          text=True, capture_output=True, timeout=10, check=False)


class GuestIsolationLibTests(unittest.TestCase):
    def test_defaults_keep_historical_behaviour(self) -> None:
        result = configure({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "mode=rw suffix= net=")

    def test_read_only_caches_and_softnet_with_allow_list(self) -> None:
        result = configure({"TARTCI_HOST_CACHE_ACCESS": "ro", "TARTCI_TART_NETWORK": "softnet",
                            "TARTCI_TART_SOFTNET_ALLOW": "192.168.64.1/32,10.0.0.0/8"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "mode=ro suffix=:ro net=--net-softnet --net-softnet-allow=192.168.64.1/32,10.0.0.0/8",
        )

    def test_invalid_values_fail_closed(self) -> None:
        for env in ({"TARTCI_HOST_CACHE_ACCESS": "readonly"},
                    {"TARTCI_TART_NETWORK": "bridged"},
                    {"TARTCI_TART_NETWORK": "softnet", "TARTCI_TART_SOFTNET_ALLOW": "0.0.0.0/0 --x"}):
            with self.subTest(env=env):
                result = configure(env)
                self.assertEqual(result.returncode, 2)
                self.assertIn("invalid TARTCI_", result.stderr)


class ProviderWiringTests(unittest.TestCase):
    def test_macos_cache_shares_and_network_follow_the_knobs(self) -> None:
        body = MACOS.read_text(encoding="utf-8")
        self.assertIn('--dir="ccache:$CACHE_ROOT/ccache$cache_ro"', body)
        self.assertIn('--dir="configure-checks:$CACHE_ROOT/configure-checks$cache_ro"', body)
        boot = body.index('tart run --no-graphics "${tart_dirs[@]}"')
        self.assertIn('${TARTCI_TART_NETWORK_ARGS[@]+"${TARTCI_TART_NETWORK_ARGS[@]}"}',
                      body[boot:boot + 200])
        # Read-only guests use a private configure-check copy and a read-only
        # ccache, recorded in the runner .env the Aqua runner actually reads.
        self.assertIn("rsync -a '/Volumes/My Shared Files/configure-checks/'", body)
        self.assertIn("'CCACHE_READONLY=true'", body)
        self.assertRegex(body, r"awk -F= .*CCACHE_READONLY\|CCACHE_TEMPDIR\)\$/' \.env > \.env\.tartci")

    def test_macos_refuses_read_only_caches_with_write_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {"PATH": os.environ["PATH"], "HOME": directory,
                   "TARTCI_STATE_DIR": str(Path(directory) / "state"),
                   "TARTCI_HOST_CACHE_ACCESS": "ro", "TARTCI_CCACHE_WRITE_ISOLATION": "1"}
            result = subprocess.run(["/bin/bash", str(MACOS), "--once"], env=env, text=True,
                                    capture_output=True, timeout=30, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be combined with TARTCI_CCACHE_WRITE_ISOLATION=1", result.stderr)

    def test_linux_cache_share_network_and_binding_follow_the_knobs(self) -> None:
        body = LINUX.read_text(encoding="utf-8")
        self.assertIn('--dir="ccache:$CACHE_ROOT/ccache-linux$(tartci_host_cache_mount_suffix)"', body)
        self.assertIn('${TARTCI_TART_NETWORK_ARGS[@]+"${TARTCI_TART_NETWORK_ARGS[@]}"}', body)
        self.assertIn("bash -s -- /mnt/host/ccache '' '$TARTCI_HOST_CACHE_ACCESS_MODE'", body)
        self.assertIn("'CCACHE_READONLY=true'", body)

    def test_linux_cache_binding_accepts_a_read_only_share(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            share = Path(directory) / "mnt" / "ccache"
            share.mkdir(parents=True)
            env = {"PATH": os.environ["PATH"], "HOME": directory,
                   "TARTCI_CCACHE_MOUNT_INFO": "virtiofs com.apple.virtio-fs.automount"}
            script = ROOT / "providers" / "tart-linux" / "prepare-ccache.sh"
            for access, expected in (("ro", 0), ("rw", 0), ("bogus", 70)):
                with self.subTest(access=access):
                    result = subprocess.run(["bash", str(script), str(share), "", access], env=env,
                                            text=True, capture_output=True, timeout=10, check=False)
                    self.assertEqual(result.returncode, expected, result.stderr)

    def test_guest_ssh_never_forwards_the_operator_agent(self) -> None:
        for provider in (MACOS, LINUX, WINDOWS):
            with self.subTest(provider=provider.parent.name):
                body = provider.read_text(encoding="utf-8")
                start = body.index("SSH_OPTS=(")
                options = body[start:body.index(")", start)]
                for option in ("IdentitiesOnly=yes", "ForwardAgent=no", "ForwardX11=no"):
                    self.assertIn(option, options)

    def test_windows_proxy_command_cannot_inject_netdev_options(self) -> None:
        body = WINDOWS.read_text(encoding="utf-8")
        self.assertIn("3128-cmd:${TARTCI_WIN_PROXY_COMMAND//,/,,}", body)
        self.assertNotIn("3128-cmd:$TARTCI_WIN_PROXY_COMMAND\"", body)

    def test_windows_assigned_job_has_a_host_deadline(self) -> None:
        body = WINDOWS.read_text(encoding="utf-8")
        assigned = body.index('runner_assigned_at="$(now_epoch)"')
        deadline = body.index('-ge "$job_timeout" ]; then', assigned)
        self.assertLess(assigned, deadline)
        self.assertIn('job_timeout="${TARTCI_JOB_TIMEOUT_SECS:-21600}"', body)


class GoldenCredentialTests(unittest.TestCase):
    """Clones share their golden's credentials, so none may be well known."""

    def autounattend(self, root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        stub = root / "bin"
        stub.mkdir(exist_ok=True)
        (stub / "hdiutil").write_text("#!/bin/sh\nexit 0\n")
        (stub / "hdiutil").chmod(0o755)
        key = root / "id.pub"
        key.write_text("ssh-ed25519 AAAAfixture operator\n")
        script = ROOT / "providers" / "qemu-windows" / "make-autounattend.sh"
        return subprocess.run(
            ["bash", str(script), str(root / "out")],
            env={"PATH": f"{stub}:{os.environ['PATH']}", "HOME": str(root),
                 "TARTCI_PUBKEYS": str(key), **env},
            text=True, capture_output=True, timeout=30, check=False)

    def test_windows_golden_gets_a_private_password_and_key_only_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.autounattend(root, {})
            self.assertEqual(result.returncode, 0, result.stderr)
            xml = (root / "out" / "media" / "autounattend.xml").read_text()
            password_file = root / "out" / "admin-password"
            password = password_file.read_text().strip()
            self.assertGreaterEqual(len(password), 12)
            self.assertEqual(password_file.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("<Value>admin</Value>", xml)
            self.assertEqual(xml.count(f"<Value>{password}</Value>"), 2)
            self.assertIn("'PasswordAuthentication no'", xml)

    def test_windows_golden_rejects_a_weak_or_unsafe_password(self) -> None:
        for value in ("admin", "has space in it", "<xml>injection</xml>"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                result = self.autounattend(Path(directory), {"TARTCI_WIN_ADMIN_PASSWORD": value})
                self.assertEqual(result.returncode, 2)

    def test_linux_golden_bake_disables_password_ssh(self) -> None:
        body = (ROOT / "providers" / "tart-linux" / "provision.sh").read_text(encoding="utf-8")
        self.assertIn("/etc/ssh/sshd_config.d/00-tartci-key-only.conf", body)
        self.assertIn("'PasswordAuthentication no'", body)
        self.assertIn("sudo sshd -t", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
