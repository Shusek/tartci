#!/usr/bin/env python3
"""Regression corpus for every reviewed broad-kill command shape."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import launchctl_guard as guard
from test_launchctl_guard import ALLOWED, BLOCKED, BROAD_KILLS, PS_FIXTURE


MUST_REFUSE = tuple(dict.fromkeys(
    list(BLOCKED) + list(BROAD_KILLS) + [
        "pkill -f runner.sh --loop", "pkill -f TartCILauncher",
        "pkill -f 'zsh -c source'", "pkill -f shell-snapshots",
        "pkill -f 'zsh -lc'", "pkill -f claude-proxy", "pkill -f subrouter",
        "pkill -f tartci-launcher", "pkill -f 'codex resume'",
        "pkill -f 'sr codex'", "pkill -f 'claude proxy'", "pkill -f 'login -flp'",
        "pkill -f 'shipyard-daemon-launcher'", "pkill -f 'Runner.Listener'",
        "ssh m3 'for p in $(pgrep -f codex); do kill $p; done'",
        "ssh m3 \"kill $(pgrep -f 'sr codex')\"",
        "ssh m3 'kill $(pgrep -f tartci-launcher)'",
        "bash -c \"kill $(pgrep -f 'sr codex')\"",
        "eval \"kill $(pgrep -f tartci-launcher)\"",
        "pgrep -f 'sr codex' | xargs sh -c 'kill $0'",
        "perl -e 'kill 9, grep(/sr codex/, qx(pgrep -f sr codex))'",
        "python3 -c \"os.kill(1, 9) # pgrep -f sr codex\"",
        "awk 'system(\"kill $(pgrep -f tartci-launcher)\")'",
        "ps aux | grep tartci-launcher | awk '{print $2}' | xargs kill -9",
        "kill $(ps aux | grep tartci-launcher | awk '{print $2}')",
        "PAT=tartci-launcher; kill $(pgrep -f $PAT)",
    ]
))

MUST_ALLOW = tuple(dict.fromkeys(list(ALLOWED) + [
    "pgrep -f 'sr codex'", "pidof tartci-launcher",
    "grep -i kill notes.txt | less", "grep pkill README.md | cat",
    "ps aux | grep 'kill' | less", "grep -rn 'pkill -f' scripts/",
]))


def _rows(path: Path) -> list[guard.ProcessRow]:
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = guard.PROCESS_ROW.match(line)
        if match:
            result.append(guard.ProcessRow(int(match.group(1)), int(match.group(2)), match.group(3)))
    return result


FULL_ROWS = _rows(PS_FIXTURE)
PARTIAL_ROWS = FULL_ROWS[:40]
TABLE_STATES = {
    "m3": FULL_ROWS,
    "m5": FULL_ROWS,
    "partial-40": PARTIAL_ROWS,
    "no-table": None,
    "slow-ps": None,
}


class ReviewedCommandCorpus(unittest.TestCase):
    def test_every_must_refuse_shape_across_every_table_state(self) -> None:
        for state, rows in TABLE_STATES.items():
            for command in MUST_REFUSE:
                with self.subTest(state=state, command=command):
                    with mock.patch.object(guard, "_process_table", return_value=rows):
                        self.assertEqual(guard.decide(command)[0], 2)

    def test_every_must_allow_shape_across_every_table_state(self) -> None:
        for state, rows in TABLE_STATES.items():
            for command in MUST_ALLOW:
                with self.subTest(state=state, command=command):
                    with mock.patch.object(guard, "_process_table", return_value=rows):
                        self.assertEqual(guard.decide(command), (0, ""))

    def test_slow_ps_receipt_is_explicitly_represented(self) -> None:
        with mock.patch.object(guard.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("ps", 5)):
            with mock.patch.dict("os.environ", {"TARTCI_GUARD_TESTING": "1"}, clear=False):
                self.assertEqual(guard._process_table(), None)


if __name__ == "__main__":
    unittest.main()
