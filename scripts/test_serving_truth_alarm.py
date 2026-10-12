from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import serving_truth_alarm as alarm


BASE = 1_800_000_000.0


def row(ts: float, event: str, host: str = "m5", **extra: object) -> dict:
    return {"ts": alarm.dt.datetime.fromtimestamp(ts, alarm.dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"), "event": event, "runner": host, **extra}


class ServingTruthTests(unittest.TestCase):
    def test_queue_fetch_uses_one_paginated_api_read_and_keeps_required_rows(self) -> None:
        payload = {"workflow_runs": [{"id": 1}, {"id": 2, "required": False}]}
        with mock.patch.object(alarm.subprocess, "run", return_value=mock.Mock(
                returncode=0, stdout=json.dumps(payload), stderr="")) as run:
            self.assertEqual(alarm.fetch_required_queue("ghapp", "danielraffel/tartci"), [{"id": 1}])
        run.assert_called_once_with(
            ["ghapp", "api", "repos/danielraffel/tartci/actions/runs?status=queued&per_page=100"],
            capture_output=True, text=True, timeout=45, check=False)

    def test_clone_is_not_a_job_start_and_queued_work_trips_at_20_minutes(self) -> None:
        queued = [{"id": 11, "required": True,
                   "created_at": alarm.dt.datetime.fromtimestamp(
                       BASE - 1800, alarm.dt.timezone.utc).isoformat()}]
        trips = alarm.classify([row(BASE - 100, "clone_start")], queued, now=BASE)
        self.assertEqual([t["rule"] for t in trips], ["queued_required_no_job_start"])

    def test_job_assigned_is_the_distinct_start_control(self) -> None:
        queued = [{"id": 12, "created_at": alarm.dt.datetime.fromtimestamp(
            BASE - 1800, alarm.dt.timezone.utc).isoformat()}]
        self.assertEqual(alarm.classify([row(BASE - 100, "job_assigned")], queued, now=BASE), [])

    def test_admission_streak_trips_without_host_start(self) -> None:
        events = [row(BASE - 300 + i * 30, "admission_error", detail="exit 255") for i in range(3)]
        trips = alarm.classify(events, now=BASE)
        self.assertEqual([t["rule"] for t in trips], ["admission_error_streak"])
        self.assertEqual(len(trips[0]["evidence"]["errors"]), 3)

    def test_signal_death_requires_assigned_work(self) -> None:
        events = [row(BASE - 60, "job_assigned", job_id="114426391754"),
                  row(BASE - 10, "supervisor_signal", detail="INT/TERM quarantine=signal_teardown_unknown")]
        trips = alarm.classify(events, now=BASE)
        self.assertEqual([t["rule"] for t in trips], ["lane_death_by_signal"])

    def test_replay_outage_shapes_and_quiet_control(self) -> None:
        fixture = json.loads((Path(__file__).parent / "fixtures" / "serving_truth_replay.json").read_text())
        for name, recorded in fixture["cases"].items():
            with self.subTest(case=name):
                self.assertTrue(alarm.classify(recorded, fixture["queued"], now=fixture["now"]), name)
        self.assertEqual(alarm.classify(fixture["quiet"], [], now=fixture["now"]), [])

        # O3/O9/O10/O11 all have the same serving truth: required work exists,
        # while the lanes emit no starts.  O3 additionally supplies admission
        # evidence, and O9 supplies the signal-death evidence.
        queued = [{"id": n, "required": True,
                   "created_at": alarm.dt.datetime.fromtimestamp(
                       BASE - 3600, alarm.dt.timezone.utc).isoformat()} for n in range(4)]
        outage = [row(BASE - 900, "admission_error", host="m5s"),
                  row(BASE - 840, "admission_error", host="m5s"),
                  row(BASE - 780, "admission_error", host="m5s")]
        rules = {trip["rule"] for trip in alarm.classify(outage, queued, now=BASE)}
        self.assertEqual(rules, {"queued_required_no_job_start", "admission_error_streak"})
        signal_rules = alarm.classify([
            row(BASE - 30, "job_assigned", host="m3", job_id="o9"),
            row(BASE - 10, "supervisor_signal", host="m3", detail="INT/TERM quarantine=signal_teardown_unknown")
        ], queued, now=BASE)
        self.assertEqual([trip["rule"] for trip in signal_rules], ["lane_death_by_signal"])
        quiet = [row(BASE - 100, "clone_start"), row(BASE - 80, "boot_ip")]
        self.assertEqual(alarm.classify(quiet, [], now=BASE), [])

    def test_alert_writes_receipt_and_deduplicates_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = root / "events.jsonl"
            events.write_text(json.dumps(row(BASE - 30, "admission_error")) + "\n" +
                              json.dumps(row(BASE - 20, "admission_error")) + "\n" +
                              json.dumps(row(BASE - 10, "admission_error")) + "\n")
            opened: list[str] = []
            issue = lambda title, body: (opened.append(title) or (0, "91"))
            first = alarm.alert_pass(root / "state", events, [], now=BASE, issue=issue)
            second = alarm.alert_pass(root / "state", events, [], now=BASE + 60, issue=issue)
            self.assertTrue(first["receipts"])
            self.assertEqual(len(opened), 1)
            self.assertFalse(second["evented"])


if __name__ == "__main__":
    unittest.main()
