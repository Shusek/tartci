#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "runner_group_repository_access.py"


FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
from urllib.parse import parse_qs, urlparse

with open(os.environ["ACCESS_STATE"], encoding="utf-8") as handle:
    state = json.load(handle)
with open(os.environ["ACCESS_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "argv": sys.argv[1:],
        "repo": os.environ.get("SHIPYARD_GH_APP_REPO"),
        "gh_repo": os.environ.get("GH_REPO"),
    }) + "\n")
path = sys.argv[-1]
if state.get("api_error"):
    print("gh: Resource not accessible by integration (HTTP 403)", file=sys.stderr)
    raise SystemExit(1)
if path.endswith("/actions/permissions/fork-pr-contributor-approval"):
    print(json.dumps({"approval_policy": state.get("approval", "first_time_contributors")}))
    raise SystemExit(0)
if path.startswith("repos/"):
    public = path[len("repos/"):] in state.get("public", [])
    print(json.dumps(state.get("repository") or {
        "private": not public, "visibility": "public" if public else "private"}))
    raise SystemExit(0)
if "/repositories?" not in path:
    group = {"id": 3, "visibility": state.get("visibility", "selected")}
    group.update(state.get("group", {}))
    print(json.dumps(group))
    raise SystemExit(0)
parsed = urlparse("https://example.invalid/" + path)
page = int(parse_qs(parsed.query).get("page", ["1"])[0])
pages = state.get("pages", [[]])
repositories = pages[page - 1] if page <= len(pages) else []
print(json.dumps({
    "total_count": sum(len(items) for items in pages),
    "repositories": [{"full_name": name} for name in repositories],
}))
'''


class RunnerGroupRepositoryAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gh = self.root / "fake-gh"
        self.gh.write_text(FAKE_GH, encoding="utf-8")
        self.gh.chmod(self.gh.stat().st_mode | stat.S_IXUSR)
        self.state = self.root / "state.json"
        self.calls = self.root / "calls.jsonl"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_check(
        self, group: int, state: dict, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.state.write_text(json.dumps(state), encoding="utf-8")
        env = os.environ.copy()
        for name in (
            "TARTCI_ALLOW_PUBLIC_REPOSITORY_RUNNERS", "TARTCI_REQUIRE_WORKFLOW_RESTRICTION",
            "TARTCI_QUEUE_POLICY_FILE", "TARTCI_RUNNER_SCOPE", "TARTCI_ASSIGNMENT_REPOSITORIES",
        ):
            env.pop(name, None)
        env.update(extra_env or {})
        env["ACCESS_STATE"] = str(self.state)
        env["ACCESS_CALLS"] = str(self.calls)
        return subprocess.run(
            [
                "python3", str(CHECK),
                "--repo", "Generous-Corp/pulp",
                "--runner-group-id", str(group),
                "--gh-cli", str(self.gh),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def calls_made(self) -> list[str]:
        if not self.calls.exists():
            return []
        return [json.loads(line)["argv"][-1] for line in self.calls.read_text().splitlines()]

    def test_repository_scoped_registration_is_intrinsically_visible(self) -> None:
        result = self.run_check(1, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["registration_scope"], "repository")
        self.assertEqual(receipt["public_repositories"], [])
        # Only the repository's own visibility is read; no org policy.
        self.assertEqual(self.calls_made(), ["repos/Generous-Corp/pulp"])

    def test_repository_scope_fails_closed_when_visibility_is_unreadable(self) -> None:
        result = self.run_check(1, {"api_error": True})
        self.assertEqual(result.returncode, 2)
        self.assertIn("access error", result.stderr)
        # An answer that proves neither visibility is a denial, not a retry.
        for repository in ({"visibility": "selected"}, {"id": 1}):
            with self.subTest(repository=repository):
                result = self.run_check(1, {"repository": repository})
                self.assertEqual(result.returncode, 3)
                self.assertIn("unreadable visibility", json.loads(result.stdout)["reason"])

    def test_visibility_falls_back_to_the_private_flag(self) -> None:
        result = self.run_check(1, {"repository": {"private": True}})
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_check(1, {"repository": {"private": False}})
        self.assertEqual(result.returncode, 3)
        self.assertIn("fork pull requests run without approval", json.loads(result.stdout)["reason"])

    def test_public_repository_requires_approval_for_all_external_contributors(self) -> None:
        for approval in ("first_time_contributors", "first_time_contributors_new_to_github"):
            with self.subTest(approval=approval):
                result = self.run_check(1, {"public": ["Generous-Corp/pulp"], "approval": approval})
                self.assertEqual(result.returncode, 3)
                receipt = json.loads(result.stdout)
                self.assertEqual(receipt["verdict"], "deny")
                self.assertIn("fork pull requests run without approval", receipt["reason"])

    def test_public_repository_with_full_fork_approval_is_admitted(self) -> None:
        result = self.run_check(
            1, {"public": ["Generous-Corp/pulp"], "approval": "all_external_contributors"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["public_repositories"], ["Generous-Corp/pulp"])

    def test_public_repository_exception_is_explicit(self) -> None:
        result = self.run_check(
            1,
            {"public": ["Generous-Corp/pulp"]},
            {"TARTCI_ALLOW_PUBLIC_REPOSITORY_RUNNERS": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(
            "repos/Generous-Corp/pulp/actions/permissions/fork-pr-contributor-approval",
            self.calls_made(),
        )

    def test_group_that_denies_public_repositories_needs_no_fork_approval(self) -> None:
        result = self.run_check(
            3,
            {"pages": [["Generous-Corp/pulp"]], "public": ["Generous-Corp/pulp"],
             "group": {"allows_public_repositories": False}},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertFalse(receipt["allows_public_repositories"])
        self.assertEqual(receipt["public_repositories"], [])

    def test_group_that_allows_public_repositories_checks_fork_approval(self) -> None:
        # An absent flag is treated as allowing public repositories.
        for group in ({"allows_public_repositories": True}, {}):
            with self.subTest(group=group):
                result = self.run_check(
                    3,
                    {"pages": [["Generous-Corp/pulp"]], "public": ["Generous-Corp/pulp"],
                     "group": group},
                )
                self.assertEqual(result.returncode, 3)
                self.assertEqual(json.loads(result.stdout)["verdict"], "deny")

    def test_required_workflow_restriction_refuses_repository_scope(self) -> None:
        result = self.run_check(1, {}, {"TARTCI_REQUIRE_WORKFLOW_RESTRICTION": "1"})
        self.assertEqual(result.returncode, 3)
        self.assertIn("cannot be restricted to workflows", json.loads(result.stdout)["reason"])

    def test_required_workflow_restriction_checks_the_group_allow_list(self) -> None:
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({
            "repository": "Generous-Corp/pulp", "events": ["push"],
            "workflow_paths": [".github/workflows/ci.yml"],
        }), encoding="utf-8")
        env = {"TARTCI_REQUIRE_WORKFLOW_RESTRICTION": "1", "TARTCI_QUEUE_POLICY_FILE": str(policy)}
        cases = {
            "unrestricted": ({"restricted_to_workflows": False}, 3),
            "outside policy": ({"restricted_to_workflows": True, "selected_workflows": [
                "Generous-Corp/pulp/.github/workflows/release.yml@refs/heads/main"]}, 3),
            "foreign repository": ({"restricted_to_workflows": True, "selected_workflows": [
                "Generous-Corp/forge/.github/workflows/ci.yml@refs/heads/main"]}, 3),
            "unpinned": ({"restricted_to_workflows": True, "selected_workflows": [
                "Generous-Corp/pulp/.github/workflows/ci.yml"]}, 3),
            "wildcard ref": ({"restricted_to_workflows": True, "selected_workflows": [
                "Generous-Corp/pulp/.github/workflows/ci.yml@*"]}, 3),
            "wildcard file": ({"restricted_to_workflows": True, "selected_workflows": [
                "Generous-Corp/pulp/.github/workflows/*.yml@refs/heads/main"]}, 3),
            "pull request ref": ({"restricted_to_workflows": True, "selected_workflows": [
                "Generous-Corp/pulp/.github/workflows/ci.yml@refs/pull/1/merge"]}, 3),
            "commit": ({"restricted_to_workflows": True, "selected_workflows": [
                "Generous-Corp/pulp/.github/workflows/ci.yml@" + "a" * 40]}, 0),
            "exact": ({"restricted_to_workflows": True, "selected_workflows": [
                "Generous-Corp/pulp/.github/workflows/ci.yml@refs/heads/main"]}, 0),
        }
        for name, (group, expected) in cases.items():
            with self.subTest(case=name):
                result = self.run_check(3, {"pages": [["Generous-Corp/pulp"]], "group": group}, env)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["selected_workflows"],
            ["Generous-Corp/pulp/.github/workflows/ci.yml@refs/heads/main"],
        )

    def test_selected_org_group_must_include_the_repository(self) -> None:
        result = self.run_check(3, {"pages": [["Generous-Corp/forge"]]})
        self.assertEqual(result.returncode, 3)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["verdict"], "deny")
        self.assertIn("must select only", receipt["reason"])

    def test_selected_org_group_rejects_cross_repository_assignment_scope(self) -> None:
        result = self.run_check(
            3,
            {"pages": [["Generous-Corp/forge"], ["Generous-Corp/pulp"]]},
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["verdict"], "deny")
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call["repo"] == "Generous-Corp/pulp" for call in calls))
        self.assertTrue(all(call["gh_repo"] == "Generous-Corp/pulp" for call in calls))

    def test_selected_org_group_admits_only_exact_single_repository(self) -> None:
        result = self.run_check(3, {"pages": [["Generous-Corp/pulp"]]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["registration_scope"],
            "organization-single-repository",
        )

    def test_unknown_policy_and_api_denial_fail_closed(self) -> None:
        for state in ({"visibility": "mystery"}, {"api_error": True}):
            with self.subTest(state=state):
                self.calls.unlink(missing_ok=True)
                result = self.run_check(3, state)
                self.assertEqual(result.returncode, 2)
                self.assertIn("access error", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
