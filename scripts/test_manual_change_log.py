#!/usr/bin/env python3
"""Who changed the pool or a lane (scripts/manual_change_log.py)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import manual_change_log as mcl  # noqa: E402


class Tests(unittest.TestCase):
    def test_the_parent_chain_is_walked_to_launchd_and_bounded(self) -> None:
        table = {900: (800, "tartci pool off --force"), 800: (700, "-bash"),
                 700: (1, "sshd: daniel@ttys004")}
        chain = mcl.parents(900, ps=lambda pid: table.get(pid))
        self.assertEqual([c["command"] for c in chain],
                         ["tartci pool off --force", "-bash", "sshd: daniel@ttys004"])
        loop = mcl.parents(5, ps=lambda pid: (pid + 1, "x"))
        self.assertEqual(len(loop), mcl.MAX_PARENTS)

    def test_the_caller_is_named(self) -> None:
        self.assertEqual(mcl.caller({"SSH_CONNECTION": "192.0.2.9 1 192.0.2.5 22"})["via"], "ssh")
        self.assertEqual(mcl.caller({"SSH_CONNECTION": "192.0.2.9 1 192.0.2.5 22"})["ssh_client"],
                         "192.0.2.9")
        self.assertEqual(mcl.caller({"TARTCI_LAUNCHD": "1"})["via"], "launchd")
        launchd = mcl.caller({"XPC_SERVICE_NAME": "com.danielraffel.tartci.self-update",
                              "SSH_CONNECTION": "192.0.2.9 1 192.0.2.5 22"})
        self.assertEqual((launchd["via"], launchd["launchd_job"]),
                         ("launchd", "com.danielraffel.tartci.self-update"))
        self.assertEqual(mcl.caller({})["via"], "terminal")

    def test_a_row_is_appended_and_an_unwritable_log_never_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "logs" / "manual-changes.log"
            with mock.patch.dict(os.environ, {"TARTCI_MANUAL_CHANGE_LOG": str(log)}):
                mcl.record("pool off", "refused", ["--plan"], env={}, start=1)
                mcl.record("pool on", "requested", [], env={}, start=1)
            rows = [json.loads(x) for x in log.read_text().splitlines()]
            self.assertEqual([r["action"] for r in rows], ["pool off", "pool on"])
            (Path(td) / "ro").mkdir(mode=0o500)
            with mock.patch.dict(os.environ, {"TARTCI_MANUAL_CHANGE_LOG":
                                              str(Path(td) / "ro" / "x" / "log")}):
                mcl.record("pool off", "x", [], env={}, start=1)


class Redaction(unittest.TestCase):
    PLANTED = {
        "GH_TOKEN=ghp_abcdefghij0123456789 gh api repos/x": "ghp_abcdefghij0123456789",
        'curl -H "Authorization: token gho_ABCDEFGHIJ0123456789" https://api.github.com':
            "gho_ABCDEFGHIJ0123456789",
        'curl -H "Authorization: Bearer eyJhbGciOi.payload.sig" u': "eyJhbGciOi.payload.sig",
        "curl -H 'Authorization: Basic dXNlcjpwYXNzd29yZA==' u": "dXNlcjpwYXNzd29yZA==",
        "env SHIPYARD_GITHUB_APP_PRIVATE_KEY='-----BEGIN' MY_SECRET=s3cr3t run": "s3cr3t",
        "tool --password hunter2 --api-key=k3y": "hunter2",
        "echo github_pat_11ABCDEFGHIJ_klmnopq and ghs_abcdefghijklmnop": "ghs_abcdefghijklmnop",
        "git clone https://user:hunter2url@example.com/x.git": "hunter2url",
        "git clone https://oauth2:glpat-SECRETtoken@gitlab.com/x.git": "glpat-SECRETtoken",
        "security unlock-keychain -p hunter2pass /Users/x/signing.keychain-db": "hunter2pass",
        "curl -u admin:hunter2curl https://api.example.com": "hunter2curl",
        "AWS_SECRET_ACCESS_KEY=awsSecretValue aws s3 ls": "awsSecretValue",
    }

    def test_planted_secrets_never_reach_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "manual-changes.log"
            # One record per planted command, each as the caller's parent: the
            # chain is capped at MAX_PARENTS, so one long chain would drop some.
            with mock.patch.dict(os.environ, {"TARTCI_MANUAL_CHANGE_LOG": str(log)}):
                for command in self.PLANTED:
                    mcl.record("pool off", "forced", ["--force"], env={}, start=900,
                               ps=lambda p, c=command: (1, c) if p == 900 else None)
                mcl.record("pool off", "forced", ["--force", "GH_TOKEN=ghp_zzzzzzzzzz0123456789"],
                           env={}, start=1)
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(rows), len(self.PLANTED) + 1)
            self.assertTrue(all(r["parents"] for r in rows[:-1]))
            text = log.read_text()
            for command, secret in self.PLANTED.items():
                with self.subTest(command=command[:30]):
                    self.assertNotIn(secret, text)
            self.assertNotIn("ghp_zzzzzzzzzz0123456789", text)
            self.assertNotIn("k3y", text)
            self.assertNotIn("github_pat_11ABCDEFGHIJ_klmnopq", text)
            self.assertIn("[redacted]", text)
            # Control: an ordinary command line is kept whole.
            for plain in ("tartci pool off --force --plan", "ssh-keyscan -T 5 m5-lan",
                          "git push --no-verify origin main",
                          "ssh -o StrictHostKeyChecking=accept-new m5",
                          "codesign --keychain /Users/x/pulp.keychain-db -s ID a.app",
                          "SHIPYARD_GITHUB_APP_PRIVATE_KEY_PATH=/x/key.pem shipyard pr",
                          "sort -u names.txt", "git clone https://github.com/o/r.git"):
                self.assertEqual(mcl.redact(plain), plain)

    def test_the_log_is_owner_only_and_an_existing_one_is_tightened(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "manual-changes.log"
            with mock.patch.dict(os.environ, {"TARTCI_MANUAL_CHANGE_LOG": str(log)}):
                mcl.record("pool on", "requested", [], env={}, start=1)
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
                log.chmod(0o644)
                mcl.record("pool on", "requested", [], env={}, start=1)
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(log.read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
