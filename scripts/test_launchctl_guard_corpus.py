#!/usr/bin/env python3
"""Regression corpus for every reviewed broad-kill command shape."""

from __future__ import annotations

import subprocess
import sys
import unittest
import contextlib
import io
import runpy
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import launchctl_guard as guard
from test_launchctl_guard import PS_FIXTURE


_probe_path = Path("/Volumes/Workshop/Code/agent-worktrees/infra-review3/tc487/probe5.py")
_old_argv = sys.argv
sys.argv = [str(_probe_path), str(Path(__file__).resolve().parent)]
with contextlib.redirect_stdout(io.StringIO()):
    _probe5 = runpy.run_path(str(_probe_path),
                             init_globals={"UNSET_PATTERN": "UNSET_PATTERN", "NAME": "NAME"})
sys.argv = _old_argv

_PROBE6_NEW = (
    "ssh m3 '/usr/bin/kill $(pgrep -f \"sr codex\")'",
    "ssh m3 'nice -n 5 kill $(pgrep -f tartci-launcher)'",
    "ssh m3 'exec kill -9 $(pgrep -f \"sr codex\")'",
    "ssh m3 'command kill $(pgrep -f tartci-launcher)'",
    "pkill codex", "killall codex", "pkill tartci-launcher", "killall -m 'tartci.*'",
    "pkill -x sr", "ssh m3 'killall -m \"tartci.*\"'", "ssh m3 pkill codex",
    "kill -9 $(pgrep -x codex)", "kill $(pgrep codex)",
)
MUST_REFUSE = tuple(dict.fromkeys(list(_probe5["must_refuse"]) + list(_PROBE6_NEW)))
MUST_ALLOW = tuple(_probe5["read_only"])


def _rows(path: Path) -> list[guard.ProcessRow]:
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = guard.PROCESS_ROW.match(line)
        if match:
            result.append(guard.ProcessRow(int(match.group(1)), int(match.group(2)), match.group(3)))
    return result


FULL_ROWS = _rows(PS_FIXTURE)
PS_M5 = Path(__file__).resolve().parent / "fixtures" / "launchctl_guard_ps_m5.txt"
M5_ROWS = _rows(PS_M5)
PARTIAL_ROWS = M5_ROWS[:100]
TABLE_STATES = {
    "m3": FULL_ROWS,
    "m5": M5_ROWS,
    "partial-100": PARTIAL_ROWS,
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
