"""Policy filtering, cache separation, and compatibility without a VM or API."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from queue_policy import QueuePolicy
import test_paginated_queue_scan as paginated_tests


POLICY = {
    "repository": "owner/repo",
    "events": ["workflow_dispatch", "pull_request", "push"],
    "workflow_paths": [".github/workflows/ci.yml"],
    "same_repository_pull_requests": True,
    "push_branches": ["main"],
}


def run(event: str = "workflow_dispatch", run_id: int = 1) -> dict:
    return {
        "id": run_id,
        "name": "A renamed workflow",
        "path": ".github/workflows/ci.yml",
        "event": event,
        "repository": {"id": 101, "full_name": "owner/repo"},
        "head_repository": {"id": 101, "full_name": "owner/repo"},
        "head_branch": "main",
        "pull_requests": [{"head": {"repo": {"id": 101}}, "base": {"repo": {"id": 101}}}],
        "created_at": "2026-01-01T00:00:00Z",
    }


class QueuePolicyTests(unittest.TestCase):
    def test_allowed_events_and_exact_paths(self) -> None:
        policy = QueuePolicy(POLICY, "owner/repo")
        for event in POLICY["events"]:
            self.assertTrue(policy.allows(run(event)))
        self.assertTrue(policy.allows({**run(), "path": ".github/workflows/ci.yml@main"}))
        for changes in (
            {"event": "pull_request_target"},
            {"event": "schedule"},
            {"path": ".github/workflows/other.yml"},
            {"repository": {"id": 101, "full_name": "other/repo"}},
            {"event": []}, {"path": {}},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(policy.allows({**run(), **changes}))

    def test_push_is_main_and_same_repository(self) -> None:
        policy = QueuePolicy(POLICY, "owner/repo")
        self.assertFalse(policy.allows({**run("push"), "head_branch": "feature"}))
        self.assertFalse(policy.allows({**run("push"), "head_repository": {"full_name": "fork/repo"}}))

    def test_fork_and_missing_pr_evidence_fail_closed(self) -> None:
        policy = QueuePolicy(POLICY, "owner/repo")
        variants = [
            {"head_repository": {"id": 999, "full_name": "fork/repo"}},
            {"head_repository": {"id": 999, "full_name": "owner/repo"}},
            {"pull_requests": []}, {"pull_requests": None},
            {"pull_requests": [{"head": {"repo": {"id": 999}}, "base": {"repo": {"id": 101}}}]},
            {"pull_requests": [{"head": {"repo": {"id": 101}}, "base": {"repo": {"id": 999}}}]},
            {"pull_requests": [{"head": None, "base": {"repo": {"id": 101}}}]},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                self.assertFalse(policy.allows({**run("pull_request"), **changes}))

    def test_invalid_policy_is_rejected(self) -> None:
        for changes in (
            {"repository": "other/repo"}, {"events": []}, {"events": "push"},
            {"workflow_paths": []}, {"workflow_paths": ["ci.yml"]},
            {"workflow_paths": [".github/workflows/ci.yml@main"]},
            {"same_repository_pull_requests": "yes"}, {"unknown_setting": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                QueuePolicy({**POLICY, **changes}, "owner/repo")

    def test_semantically_equal_policy_has_same_fingerprint(self) -> None:
        reverse = {**POLICY, "events": list(reversed(POLICY["events"]))}
        self.assertEqual(QueuePolicy(POLICY, "OWNER/REPO").fingerprint, QueuePolicy(reverse, "owner/repo").fingerprint)


class QueuePolicyScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.receipt = patch.dict(os.environ, {"TARTCI_GH_IDENTITY_RECEIPT_TTL_SECS": "0"})
        self.receipt.start()
        self.addCleanup(self.receipt.stop)

    def scanner(self, root: Path, runs: list[dict], jobs: list[dict] | None = None, policy: dict | None = None):
        filename = root / "policy.json"
        filename.write_text(json.dumps(POLICY if policy is None else policy))
        self.calls = []
        def api(path: str) -> dict:
            self.calls.append(path)
            if path == "rate_limit":
                return paginated_tests.AUTHENTICATED_RATE_LIMIT
            if "status=queued" in path:
                return {"workflow_runs": runs}
            if "status=pending" in path or "status=in_progress" in path:
                return {"workflow_runs": []}
            if "/jobs?" in path:
                return {"jobs": jobs if jobs is not None else [{"id": 42, "status": "queued", "labels": ["self-hosted", "macOS", "ARM64", "pulp-build-vm"]}]}
            raise AssertionError(f"unexpected API path: {path}")
        return paginated_tests.PaginatedQueueScanTests()._scanner(
            "tart-macos", root / "state.json", api,
            policy_file=str(filename), workflow="The old display name",
        )

    def test_disallowed_runs_do_not_consume_job_fetch_quota(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            denied = [{**run(run_id=index), "event": "schedule"} for index in range(1, 40)]
            scanner = self.scanner(root, [*denied, run(run_id=100)])
            self.assertEqual(scanner.scan(), 1)
            jobs = [path for path in self.calls if "/jobs?" in path]
            self.assertEqual(jobs, ["repos/owner/repo/actions/runs/100/jobs?filter=latest&per_page=100"])
            self.assertFalse(any("/workflows" in path for path in self.calls))

    def test_job_requiring_extra_label_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scanner = self.scanner(Path(directory), [run()], jobs=[
                {"status": "queued", "labels": ["pulp-build-vm", "unprovided-label"]}
            ])
            self.assertEqual(scanner.scan(), 0)

    def test_policy_change_separates_discovery_and_negative_caches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.scanner(root, [run("push")])
            self.assertEqual(first.scan(), 1)
            changed = self.scanner(root, [run("push")], policy={**POLICY, "push_branches": ["release"]})
            self.assertNotEqual(first.state_path, changed.state_path)
            self.assertNotEqual(first.discovery_path, changed.discovery_path)
            self.assertEqual(changed.scan(), 0)

    def test_cached_run_is_revalidated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scanner = self.scanner(Path(directory), [])
            cached = {**run(), "event": "schedule", "_jobs": [{"status": "queued", "labels": ["pulp-build-vm"]}]}
            scanner._discover = lambda: [cached]
            self.assertEqual(scanner.scan(), 0)

    def test_missing_or_invalid_policy_never_contacts_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for body in ("{", json.dumps({**POLICY, "repository": "other/repo"})):
                filename = root / "bad.json"
                filename.write_text(body)
                with self.subTest(body=body), self.assertRaises(ValueError):
                    paginated_tests.PaginatedQueueScanTests()._scanner(
                        "tart-macos", root / "state.json", lambda _: self.fail("unexpected API"), policy_file=str(filename)
                    )

    def test_cli_policy_argument_and_provider_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "policy.json"
            policy.write_text(json.dumps(POLICY))
            payload = root / "runs.json"
            payload.write_text(json.dumps({"workflow_runs": [run()]}))
            gh = root / "fake-gh"
            gh.write_text('''#!/usr/bin/env python3
import json, os, sys
path = sys.argv[-1]
if path == "rate_limit":
    print(json.dumps({"resources": {"core": {"limit": 15000, "remaining": 14999}}}))
elif "status=queued" in path:
    print(open(os.environ["RUNS"]).read())
elif "status=pending" in path or "status=in_progress" in path:
    print('{"workflow_runs": []}')
elif "/jobs?" in path:
    print('{"jobs": [{"id": 42, "status": "queued", "labels": ["self-hosted", "macOS", "ARM64", "pulp-build-vm"]}]}')
else:
    raise SystemExit("unexpected API path: " + path)
''')
            gh.chmod(0o700)
            base = [sys.executable, str(Path(__file__).with_name("queue_scan.py")),
                    "--repo", "owner/repo", "--labels", "self-hosted,macOS,ARM64,pulp-build-vm",
                    "--state-file", str(root / "state.json"), "--shared-cache-file", str(root / "discovery.json"),
                    "--observation-lock-file", str(root / "observation.lock"), "--gh-cli", str(gh),
                    "--stagger-max-seconds", "0"]
            for provider in ("tart-macos", "qemu-windows"):
                for via_env in (False, True):
                    with self.subTest(provider=provider, via_env=via_env):
                        env = {"PATH": os.environ["PATH"], "HOME": directory, "RUNS": str(payload), "TARTCI_GH_IDENTITY_RECEIPT_TTL_SECS": "0"}
                        options = []
                        if via_env:
                            env["TARTCI_QUEUE_POLICY_FILE"] = str(policy)
                            options = ["--workflow", "Ignored legacy name"]
                        else:
                            options = ["--policy-file", str(policy)]
                        result = subprocess.run(base + ["--provider", provider] + options, env=env, text=True, capture_output=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), "1")


class AssignmentModeCompatibilityTests(unittest.TestCase):
    def test_event_class_v2_refuses_a_policy_it_would_ignore(self) -> None:
        # assignment_scan.py has no policy support, so the combination must
        # fail loudly instead of silently dropping the admission rules.
        runner = Path(__file__).resolve().parents[1] / "providers/tart-macos/runner.sh"
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / "policy.json"
            policy.write_text(json.dumps({**POLICY, "repository": "Generous-Corp/pulp"}))
            env = {"PATH": os.environ["PATH"], "HOME": directory,
                   "TARTCI_STATE_DIR": str(Path(directory) / "state"),
                   "TARTCI_QUEUE_POLICY_FILE": str(policy),
                   "TARTCI_RUNNER_ASSIGNMENT_MODE": "event-class-v2"}
            result = subprocess.run(["/bin/bash", str(runner), "--once"], env=env,
                                    text=True, capture_output=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not supported with TARTCI_RUNNER_ASSIGNMENT_MODE=event-class-v2", result.stderr)


if __name__ == "__main__":
    unittest.main()
