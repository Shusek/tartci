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


if __name__ == "__main__":
    unittest.main()
