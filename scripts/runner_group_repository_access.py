#!/usr/bin/env python3
"""Fail-closed repository-access admission for GitHub JIT runner groups.

GitHub, not tartci, decides which queued job a registered runner receives. A
runner is therefore only as trusted as the least trusted workflow that can
target its labels. Besides the runner-group repository boundary, this check
refuses a public repository whose fork pull requests can run without a
maintainer's approval, and can require a GitHub-enforced workflow allow-list.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any

from queue_policy import QueuePolicy


REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# A selected workflow must name one file at one immutable or protected ref: an
# unpinned or wildcard entry admits a pull request's own copy of the workflow.
WORKFLOW = re.compile(
    r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/"
    r"(?P<path>\.github/workflows/[^/@*?\[\]]+\.ya?ml)"
    r"@(?P<ref>refs/(?:heads|tags)/[^*?\[\]\s]+|[0-9a-f]{40})$"
)
PER_PAGE = 100
MAX_PAGES = 20
# The only fork pull request approval policy under which an outside
# contributor cannot run code on a self-hosted runner without a maintainer.
REQUIRED_FORK_APPROVAL = "all_external_contributors"


class AccessError(RuntimeError):
    pass


class RepositoryInaccessible(AccessError):
    pass


def api(gh_cli: str, path: str, repo: str, timeout: int) -> dict[str, Any]:
    env = os.environ.copy()
    env["SHIPYARD_GH_APP_REPO"] = repo
    env["GH_REPO"] = repo
    result = subprocess.run(
        [gh_cli, "api", path],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
    )
    if result.returncode:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise AccessError(f"GitHub API failed for {path}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AccessError(f"GitHub API returned malformed JSON for {path}") from exc
    if not isinstance(payload, dict):
        raise AccessError(f"GitHub API returned a non-object for {path}")
    return payload


def _enabled(name: str) -> bool:
    return os.environ.get(name, "0") == "1"


def _require_untrusted_forks_gated(
    gh_cli: str, repo: str, repositories: list[str], timeout: int,
    group_allows_public: bool,
) -> list[str]:
    """Refuse public repositories that let fork pull requests run unapproved.

    Returns the public repositories that were admitted. A public repository's
    fork pull request can choose this runner's labels in its own workflow
    file, so the repository must require approval for every outside
    contributor. TARTCI_ALLOW_PUBLIC_REPOSITORY_RUNNERS=1 is the explicit,
    reviewed exception.
    """
    admitted: list[str] = []
    for candidate in repositories:
        payload = api(gh_cli, f"repos/{candidate}", repo, timeout)
        visibility = payload.get("visibility")
        private = payload.get("private")
        if visibility is None and isinstance(private, bool):
            # Older GitHub Enterprise Server releases only report `private`.
            visibility = "private" if private else "public"
        if visibility in ("private", "internal") and private is True:
            continue
        if visibility != "public" or private is not False:
            # A readable answer that proves neither is a denial, not a retry.
            raise RepositoryInaccessible(f"repository {candidate} has unreadable visibility")
        if not group_allows_public:
            # GitHub does not route a public repository's jobs to a group that
            # disallows public repositories, so its forks cannot reach us.
            continue
        if _enabled("TARTCI_ALLOW_PUBLIC_REPOSITORY_RUNNERS"):
            admitted.append(candidate)
            continue
        approval = api(
            gh_cli,
            f"repos/{candidate}/actions/permissions/fork-pr-contributor-approval",
            repo,
            timeout,
        ).get("approval_policy")
        if approval != REQUIRED_FORK_APPROVAL:
            raise RepositoryInaccessible(
                f"public repository {candidate} lets fork pull requests run without "
                f"approval (approval_policy={approval!r}); require approval for all "
                "external contributors or set TARTCI_ALLOW_PUBLIC_REPOSITORY_RUNNERS=1"
            )
        admitted.append(candidate)
    return admitted


def _require_workflow_allow_list(group: dict[str, Any], repositories: list[str]) -> list[str]:
    """Require the group to admit only listed workflows of observed repositories.

    Enabled by TARTCI_REQUIRE_WORKFLOW_RESTRICTION=1. With a queue policy, every
    allowed workflow must also be one of the policy's workflow paths, so GitHub
    enforces the boundary the policy only uses to decide when to boot a VM.
    """
    selected = group.get("selected_workflows")
    if group.get("restricted_to_workflows") is not True or not isinstance(selected, list) or not selected:
        raise RepositoryInaccessible(
            "runner group is not restricted to selected workflows "
            "(TARTCI_REQUIRE_WORKFLOW_RESTRICTION=1)"
        )
    policy_paths = None
    policy_file = os.environ.get("TARTCI_QUEUE_POLICY_FILE")
    if policy_file:
        try:
            policy_paths = QueuePolicy.load(policy_file, repositories[0]).paths
        except (OSError, ValueError) as exc:
            raise AccessError(f"invalid queue policy: {exc}") from exc
    allowed = {candidate.lower() for candidate in repositories}
    for workflow in selected:
        match = WORKFLOW.fullmatch(workflow) if isinstance(workflow, str) else None
        if not match:
            raise RepositoryInaccessible(
                f"runner group allows workflow {workflow!r} at an unpinned or wildcard ref; "
                "pin each to refs/heads/<branch>, refs/tags/<tag> or a commit SHA"
            )
        if match.group("repo").lower() not in allowed:
            raise RepositoryInaccessible(f"runner group allows an unobserved workflow {workflow!r}")
        if (policy_paths is not None and match.group("repo").lower() == repositories[0].lower()
                and match.group("path") not in policy_paths):
            raise RepositoryInaccessible(f"runner group allows workflow {workflow!r} outside the queue policy")
    return sorted(selected)


def verify(repo: str, runner_group_id: int, gh_cli: str, timeout: int) -> dict[str, Any]:
    declared = [r for r in os.environ.get("TARTCI_ASSIGNMENT_REPOSITORIES", "").splitlines() if r]
    if any(not REPO.fullmatch(r) or r.split("/")[0].lower() != repo.split("/")[0].lower() for r in declared):
        raise AccessError("invalid declared assignment repositories")
    if runner_group_id == 1 and os.environ.get("TARTCI_RUNNER_SCOPE", "auto") != "org":
        if _enabled("TARTCI_REQUIRE_WORKFLOW_RESTRICTION"):
            raise RepositoryInaccessible(
                "repository-scoped runners cannot be restricted to workflows; "
                "use an organization runner group"
            )
        public = _require_untrusted_forks_gated(gh_cli, repo, [repo], timeout, True)
        return {
            "schema": 1,
            "verdict": "admit",
            "repo": repo,
            "runner_group_id": runner_group_id,
            "registration_scope": "repository",
            "public_repositories": public,
            "reason": "repository-scoped JIT endpoint binds visibility to the repository",
        }

    owner = repo.split("/", 1)[0]
    group_path = f"orgs/{owner}/actions/runner-groups/{runner_group_id}"
    group = api(gh_cli, group_path, repo, timeout)
    visibility = group.get("visibility")
    if visibility == "all":
        raise RepositoryInaccessible(
            f"runner group {runner_group_id} is visible to multiple repositories; "
            "tartci assignment observation is repository-scoped"
        )
    if visibility != "selected":
        raise AccessError(
            f"runner group {runner_group_id} has unsupported visibility {visibility!r}"
        )

    seen = 0
    selected: list[str] = []
    for page in range(1, MAX_PAGES + 1):
        path = (
            f"{group_path}/repositories?per_page={PER_PAGE}&page={page}"
        )
        payload = api(gh_cli, path, repo, timeout)
        total = payload.get("total_count")
        repositories = payload.get("repositories")
        if type(total) is not int or total < 0 or not isinstance(repositories, list):
            raise AccessError("runner-group repository response has invalid pagination schema")
        for item in repositories:
            if not isinstance(item, dict) or not isinstance(item.get("full_name"), str):
                raise AccessError("runner-group repository response has malformed entries")
            seen += 1
            selected.append(item["full_name"])
        if seen >= total:
            break
        if not repositories:
            raise AccessError("runner-group repository pagination ended before total_count")
    else:
        raise AccessError("runner-group repository pagination exceeded the safety bound")

    if seen != total:
        raise AccessError(
            f"runner-group repository pagination count mismatch ({seen} != {total})"
        )
    if declared:
        expected = {r.lower() for r in declared}
        observed = {r.lower() for r in selected}
        if repo.lower() not in observed or observed != expected or len(observed) != len(selected):
            raise RepositoryInaccessible("runner group repositories differ from the complete declared observation scope")
        scope = "organization-observed-repositories"
        reason = "all visible repositories are explicitly observed; foreign assignments are quarantined"
        reachable = [repo, *sorted(r for r in selected if r.lower() != repo.lower())]
    else:
        if len(selected) != 1 or selected[0].lower() != repo.lower():
            raise RepositoryInaccessible(
                f"runner group {runner_group_id} must select only {repo}; "
                f"observed {len(selected)} selected repositories"
            )
        scope = "organization-single-repository"
        reason = "runner group is exclusively visible to this repository"
        reachable = [repo]
    workflows = (
        _require_workflow_allow_list(group, reachable)
        if _enabled("TARTCI_REQUIRE_WORKFLOW_RESTRICTION") else None
    )
    # Only an explicit false exempts public repositories from the fork gate.
    allows_public = group.get("allows_public_repositories") is not False
    public = _require_untrusted_forks_gated(gh_cli, repo, reachable, timeout, allows_public)
    receipt: dict[str, Any] = {
        "schema": 1,
        "verdict": "admit",
        "repo": repo,
        "runner_group_id": runner_group_id,
        "registration_scope": scope,
        "visibility": visibility,
        "allows_public_repositories": allows_public,
        "public_repositories": public,
        "restricted_to_workflows": group.get("restricted_to_workflows") is True,
        "reason": reason,
    }
    if declared:
        receipt["assignment_repositories"] = sorted(selected)
    if workflows is not None:
        receipt["selected_workflows"] = workflows
    return receipt


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--runner-group-id", required=True, type=int)
    parser.add_argument("--gh-cli", default=os.environ.get("TARTCI_JIT_GH_CLI") or "gh")
    parser.add_argument("--timeout-seconds", type=int, default=20)
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo):
        parser.error("--repo must be OWNER/REPO")
    if args.runner_group_id < 1:
        parser.error("--runner-group-id must be positive")
    if not args.gh_cli or any(char.isspace() for char in args.gh_cli):
        parser.error("--gh-cli must be one executable path or name")
    if not 1 <= args.timeout_seconds <= 120:
        parser.error("--timeout-seconds must be 1..120")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        receipt = verify(
            args.repo, args.runner_group_id, args.gh_cli, args.timeout_seconds
        )
    except RepositoryInaccessible as exc:
        print(
            json.dumps(
                {
                    "schema": 1,
                    "verdict": "deny",
                    "repo": args.repo,
                    "runner_group_id": args.runner_group_id,
                    "reason": str(exc),
                },
                sort_keys=True,
            )
        )
        print(f"runner repository access denied: {exc}", file=sys.stderr)
        return 3
    except (AccessError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"runner repository access error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
