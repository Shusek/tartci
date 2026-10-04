"""A sibling lane that exited 75 and was never respawned is started, once."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fleet_lane_discovery  # noqa: E402
import peer_respawn  # noqa: E402

DOMAIN = "gui/501"
SELF = "com.danielraffel.tartci.tart-runner-macos-fleet.studio.pulp-gate.slot2"
SIBLING = "com.danielraffel.tartci.tart-runner-macos-fleet.studio.pulp-gate"
NOW = 1_791_000_000.0


def launchctl_print(state: str, last_exit: str, pended: bool = True) -> str:
    text = f"\tstate = {state}\n\truns = 1\n"
    if pended:
        text += "\tpended nondemand spawn = inefficient\n"
    text += f"\tlast exit code = {last_exit}\n\t\tstate = active\n"
    return text


class FakeLaunchctl:
    """Answers `print` with a scripted service and records every other call."""

    def __init__(self, printed: str, rc: int = 0) -> None:
        self.printed, self.rc = printed, rc
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> tuple[int, str, str]:
        self.calls.append(cmd)
        if cmd[1] == "print":
            return self.rc, self.printed, ""
        return 0, "", ""

    def kicks(self) -> list[list[str]]:
        return [cmd for cmd in self.calls if cmd[1] == "kickstart"]


class PeerRespawnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / "pulp-gate"
        self.state_dir.mkdir()
        self.lane = fleet_lane_discovery.Lane(SIBLING, "pulp-gate", self.state_dir,
                                              "studio-pulp-gate-01")
        self.self_lane = fleet_lane_discovery.Lane(SELF, "pulp-gate-slot2", self.state_dir,
                                                   "studio-pulp-gate-slot2-02")
        self.ledger: dict = {}

    def write_state(self, phase: str, age_s: float) -> None:
        path = self.state_dir / "studio-pulp-gate-01.state.json"
        path.write_text(json.dumps({"phase": phase}))
        os.utime(path, (NOW - age_s, NOW - age_s))

    def run_pass(self, fake: FakeLaunchctl, *, now: float = NOW, participating: bool = True,
                 disabled: set[str] | None = None, lanes=None) -> list[tuple[str, str]]:
        return peer_respawn.run_pass(
            self_label=SELF, lanes=lanes or [self.self_lane, self.lane],
            participating=participating, disabled=set() if disabled is None else disabled,
            ledger=self.ledger, run=fake, now=now, domain=DOMAIN)

    # -- the kick -----------------------------------------------------------

    def test_a_stopped_exit_75_sibling_gets_exactly_one_plain_kickstart(self) -> None:
        self.write_state("stopped", 200)
        fake = FakeLaunchctl(launchctl_print("not running", "75: EX_TEMPFAIL"))
        events = self.run_pass(fake)
        # Positive control: one kickstart, of the sibling's domain/label, no -k.
        self.assertEqual(fake.kicks(), [["launchctl", "kickstart", f"{DOMAIN}/{SIBLING}"]])
        self.assertEqual([name for name, _ in events], ["peer_respawn"])
        self.assertIn("last_exit=75", events[0][1])
        self.assertIn("stopped_for=200s", events[0][1])
        self.assertIn("pended=yes", events[0][1])
        # Its own label is never inspected, let alone kicked.
        self.assertFalse(any(SELF in " ".join(call) for call in fake.calls))

    def test_the_pended_line_is_evidence_not_a_gate(self) -> None:
        self.write_state("stopped", 200)
        fake = FakeLaunchctl(launchctl_print("not running", "75: EX_TEMPFAIL", pended=False))
        events = self.run_pass(fake)
        self.assertEqual(len(fake.kicks()), 1)
        self.assertIn("pended=no", events[0][1])

    def test_spawn_scheduled_counts_as_stopped(self) -> None:
        self.write_state("stopped", 200)
        fake = FakeLaunchctl(launchctl_print("spawn scheduled", "75: EX_TEMPFAIL"))
        self.run_pass(fake)
        self.assertEqual(len(fake.kicks()), 1)

    # -- controls: no kick ---------------------------------------------------

    def test_no_kick_inside_the_grace(self) -> None:
        self.write_state("stopped", 60)
        fake = FakeLaunchctl(launchctl_print("not running", "75: EX_TEMPFAIL"))
        self.assertEqual(self.run_pass(fake), [])
        self.assertEqual(fake.kicks(), [])

    def test_no_kick_for_a_running_sibling(self) -> None:
        self.write_state("job-running", 200)
        fake = FakeLaunchctl(launchctl_print("running", "75: EX_TEMPFAIL"))
        self.assertEqual(self.run_pass(fake), [])
        self.assertEqual(fake.kicks(), [])

    def test_no_kick_after_a_clean_exit(self) -> None:
        self.write_state("stopped", 200)
        fake = FakeLaunchctl(launchctl_print("not running", "0"))
        self.assertEqual(self.run_pass(fake), [])
        self.assertEqual(fake.kicks(), [])

    def test_another_exit_code_is_reported_once_and_never_kicked(self) -> None:
        self.write_state("stopped", 200)
        fake = FakeLaunchctl(launchctl_print("not running", "1"))
        events = self.run_pass(fake)
        self.assertEqual(fake.kicks(), [])
        self.assertEqual(events, [("peer_sibling_stopped",
                                   f"label={SIBLING} last_exit=1 phase=stopped")])
        self.assertEqual(self.run_pass(fake, now=NOW + 400), [])

    def test_no_kick_for_a_disabled_lane(self) -> None:
        self.write_state("stopped", 200)
        fake = FakeLaunchctl(launchctl_print("not running", "75: EX_TEMPFAIL"))
        self.assertEqual(self.run_pass(fake, disabled={SIBLING}), [])
        self.assertEqual(fake.kicks(), [])

    def test_no_kick_when_pool_participation_is_off(self) -> None:
        self.write_state("stopped", 200)
        fake = FakeLaunchctl(launchctl_print("not running", "75: EX_TEMPFAIL"))
        self.assertEqual(self.run_pass(fake, participating=False), [])
        self.assertEqual(fake.kicks(), [])

    def test_no_kick_when_enablement_is_unknown(self) -> None:
        self.write_state("stopped", 200)
        fake = FakeLaunchctl(launchctl_print("not running", "75: EX_TEMPFAIL"))
        events = peer_respawn.run_pass(
            self_label=SELF, lanes=[self.lane], participating=True, disabled=None,
            ledger=self.ledger, run=fake, now=NOW, domain=DOMAIN)
        self.assertEqual((events, fake.kicks()), ([], []))

    def test_only_fleet_lanes_from_discovery_are_considered(self) -> None:
        # A label outside the discovered fleet set is never touched.
        fake = FakeLaunchctl(launchctl_print("not running", "75: EX_TEMPFAIL"))
        self.assertEqual(self.run_pass(fake, lanes=[self.self_lane]), [])
        self.assertEqual(fake.calls, [])

    # -- verification, rate limit, ceiling ----------------------------------

    def test_a_kick_is_confirmed_or_reported_unconfirmed_on_the_next_pass(self) -> None:
        self.write_state("stopped", 200)
        stuck = FakeLaunchctl(launchctl_print("not running", "75: EX_TEMPFAIL"))
        self.run_pass(stuck)
        events = self.run_pass(stuck, now=NOW + 30)
        self.assertEqual(events[0][0], "peer_respawn_unconfirmed")
        self.assertEqual(len(stuck.kicks()), 1, "rate limit holds inside the interval")

        self.ledger.clear()
        self.run_pass(stuck)
        self.write_state("lease-wait", 1)
        started = FakeLaunchctl(launchctl_print("running", "75: EX_TEMPFAIL"))
        events = self.run_pass(started, now=NOW + 30)
        self.assertEqual(events[0][0], "peer_respawn_confirmed")

    def test_a_lane_that_keeps_exiting_75_hits_the_ceiling_and_says_so(self) -> None:
        fake = FakeLaunchctl(launchctl_print("not running", "75: EX_TEMPFAIL"))
        events: list[tuple[str, str]] = []
        for step in range(6):
            now = NOW + step * 400
            self.write_state("stopped", 200)
            os.utime(self.state_dir / "studio-pulp-gate-01.state.json", (now - 200, now - 200))
            events += self.run_pass(fake, now=now)
        self.assertEqual(len(fake.kicks()), 3)
        names = [name for name, _ in events]
        self.assertEqual(names.count("peer_respawn_ceiling"), 1)

    def test_the_lane_loop_runs_the_tick(self) -> None:
        body = (HERE.parent / "providers" / "tart-macos" / "runner.sh").read_text()
        loop = body[body.index("  while true; do\n    if [ -n \"$CURRENT_VM\" ]; then"):]
        self.assertIn("tartci_peer_respawn_tick", loop[:3000])
        self.assertIn('--self-label "$TARTCI_LAUNCHD_LABEL"', body)



REAP = "com.danielraffel.tartci.reap"
SELF_UPDATE = "com.danielraffel.tartci.self-update"


class IntervalFake:
    """A launchctl whose interval agent has a scripted state and run count."""

    def __init__(self, state: str = "not running", runs: int = 2593) -> None:
        self.state, self.runs = state, runs
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> tuple[int, str, str]:
        self.calls.append(cmd)
        if cmd[1] == "print":
            return 0, (f"\tstate = {self.state}\n\truns = {self.runs}\n"
                       "\tpended nondemand spawn = interval\n\tlast exit code = 0\n"), ""
        return 0, "", ""

    def kicks(self) -> list[list[str]]:
        return [cmd for cmd in self.calls if cmd[1] == "kickstart"]


class IntervalAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger: dict = {}

    def run_pass(self, fake: IntervalFake, now: float, label: str = REAP,
                 log_age: float | None = None) -> list[tuple[str, str]]:
        return peer_respawn.interval_pass(
            agents=[(label, f"/LaunchAgents/{label}.plist")], ledger=self.ledger, run=fake,
            now=now, domain=DOMAIN, interval_of=lambda _plist: 300,
            log_age_of=lambda _plist: log_age)

    def observe_then_wait(self, fake: IntervalFake, label: str = REAP) -> float:
        """First observation, then a pass 3 intervals and a bit later."""
        self.assertEqual(self.run_pass(fake, NOW, label), [])
        return NOW + 3 * 300 + 1

    def test_a_stale_not_running_agent_gets_one_plain_kick(self) -> None:
        fake = IntervalFake()
        events = self.run_pass(fake, self.observe_then_wait(fake))
        self.assertEqual(fake.kicks(), [["launchctl", "kickstart", f"{DOMAIN}/{REAP}"]])
        self.assertEqual(events[0][0], "interval_respawn")
        self.assertIn("pended=yes", events[0][1])

    def test_a_stale_but_running_agent_is_never_kicked(self) -> None:
        fake = IntervalFake(state="running")
        self.assertEqual(self.run_pass(fake, self.observe_then_wait(fake)), [])
        self.assertEqual(fake.kicks(), [])

    def test_an_agent_inside_three_intervals_is_not_kicked(self) -> None:
        fake = IntervalFake()
        self.run_pass(fake, NOW)
        self.assertEqual(self.run_pass(fake, NOW + 3 * 300), [])
        self.assertEqual(fake.kicks(), [])

    def test_a_fresh_log_means_the_agent_ran(self) -> None:
        fake = IntervalFake()
        self.assertEqual(self.run_pass(fake, self.observe_then_wait(fake), log_age=60), [])
        self.assertEqual(fake.kicks(), [])

    def test_self_update_is_reported_stale_and_never_kicked(self) -> None:
        fake = IntervalFake()
        later = self.observe_then_wait(fake, SELF_UPDATE)
        events = self.run_pass(fake, later, SELF_UPDATE)
        self.assertEqual(fake.kicks(), [])
        self.assertEqual([name for name, _ in events], ["interval_agent_stale"])
        self.assertEqual(self.run_pass(fake, later + 60, SELF_UPDATE), [], "reported once")

    def test_a_second_kick_inside_one_interval_is_suppressed(self) -> None:
        fake = IntervalFake()
        later = self.observe_then_wait(fake)
        self.run_pass(fake, later)
        events = self.run_pass(fake, later + 299)
        self.assertEqual(len(fake.kicks()), 1)
        self.assertEqual([name for name, _ in events], ["interval_respawn_unconfirmed"])

    def test_a_kick_that_advances_runs_is_confirmed(self) -> None:
        fake = IntervalFake()
        later = self.observe_then_wait(fake)
        self.run_pass(fake, later)
        fake.runs += 1
        events = self.run_pass(fake, later + 300)
        self.assertEqual(events[0][0], "interval_respawn_confirmed")

    def test_the_ceiling_fires_after_three_unconfirmed_kicks(self) -> None:
        fake = IntervalFake()
        now = self.observe_then_wait(fake)
        events: list[tuple[str, str]] = []
        for step in range(6):
            events += self.run_pass(fake, now + step * 301)
        self.assertEqual(len(fake.kicks()), 3)
        names = [name for name, _ in events]
        self.assertEqual(names.count("interval_respawn_unconfirmed"), 3)
        self.assertEqual(names.count("interval_respawn_ceiling"), 1)

if __name__ == "__main__":
    unittest.main()
