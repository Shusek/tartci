#!/usr/bin/env python3
"""Hermetic tests for the bounded Tart store health probe."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store_health  # noqa: E402
import tartci_launchd_watchdog as watchdog  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        result = store_health.probe(td, timeout_s=1)
        assert result["ok"] is True, result
        assert result["device_id"]
        assert not list(Path(td).iterdir()), "probe must not create files"

    with mock.patch.object(
        store_health,
        "run_bounded",
        side_effect=store_health.ObservationError(
            "tart_store_probe", "timeout", "timed out after 0.01s"),
    ):
        result = store_health.probe("/wedged/Atelier", timeout_s=0.01)
    assert result["ok"] is False
    assert result["reason"] == "timeout after 0.01s"

    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "does-not-exist"
        result = store_health.probe(str(missing), timeout_s=1)
        assert result["ok"] is False, result
        assert not missing.exists(), "probe must not create a missing store"

    healthy = store_health.next_state({}, {"ok": True})
    assert healthy["state"] == "healthy"
    first = store_health.next_state({}, {"ok": False, "reason": "EINTR"})
    assert first["state"] == "suspect"
    second = store_health.next_state(first, {"ok": False, "reason": "timeout"})
    assert second["state"] == "draining"
    assert second["drain_required"] is True
    changed = store_health.next_state(
        {"last_result": {"ok": True, "device_id": "old"}},
        {"ok": True, "device_id": "new"}, threshold=1)
    assert changed["state"] == "draining"
    assert "device identity changed" in changed["last_result"]["reason"]

    with tempfile.TemporaryDirectory() as td:
        state_home = Path(td) / "state-home"
        participation = Path(td) / "participation"
        participation.write_text("1\n")
        with mock.patch.dict(os.environ, {"TARTCI_HOME": str(state_home)}), \
                mock.patch.object(watchdog, "resolve_tart_home", return_value=(td, "test")), \
                mock.patch.object(watchdog.store_health, "probe", side_effect=[
                    {"ok": False, "reason": "timeout"},
                    {"ok": False, "reason": "EINTR"},
                ]), \
                mock.patch.object(watchdog.subprocess, "run") as run, \
                mock.patch.object(watchdog, "pool_participating", return_value=True):
            run.return_value.returncode = 0
            watchdog.store_health_pass(
                participation_file=str(participation), status_only=False,
                dry_run=False, failure_threshold=2)
            result = watchdog.store_health_pass(
                participation_file=str(participation), status_only=False,
                dry_run=False, failure_threshold=2)
        assert result["state"] == "draining"
        assert result["action"] == "drained"
        assert any(call.args[0][1:3] == ["pool", "drain"] for call in run.call_args_list)
    print("PASS store health")


if __name__ == "__main__":
    main()
