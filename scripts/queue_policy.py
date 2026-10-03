"""Optional repository-bound admission policy for Actions queue discovery.

This controls VM demand, not GitHub's eventual runner-to-job assignment.
Runner groups and workflow labels must enforce the same routing boundaries.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


class QueuePolicy:
    def __init__(self, document: dict[str, Any], repo: str) -> None:
        fields = {"repository", "events", "workflow_paths", "same_repository_pull_requests", "push_branches"}
        if not isinstance(document, dict) or set(document) - fields:
            raise ValueError("queue policy must be an object with known fields")
        if str(document.get("repository", "")).lower() != repo.lower():
            raise ValueError("queue policy repository must match --repo")
        self.repo = repo.lower()
        self.events = self._strings(document, "events", required=True)
        self.paths = self._strings(document, "workflow_paths", required=True)
        if any(not re.fullmatch(r"\.github/workflows/[^/@]+\.ya?ml", path) for path in self.paths):
            raise ValueError("queue policy workflow_paths must be exact .github/workflows/*.yml or *.yaml paths")
        self.push_branches = self._strings(document, "push_branches", required=False)
        self.same_repo_pr = document.get("same_repository_pull_requests", False)
        if not isinstance(self.same_repo_pr, bool):
            raise ValueError("queue policy same_repository_pull_requests must be a boolean")
        canonical = {
            "repository": self.repo,
            "events": sorted(self.events),
            "workflow_paths": sorted(self.paths),
            "same_repository_pull_requests": self.same_repo_pr,
            "push_branches": sorted(self.push_branches),
        }
        self.fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _strings(document: dict[str, Any], key: str, *, required: bool) -> set[str]:
        values = document.get(key, [] if not required else None)
        if not isinstance(values, list) or (required and not values) or any(
            not isinstance(item, str) or not item or item != item.strip() for item in values
        ):
            raise ValueError(f"queue policy {key} must be {'a nonempty ' if required else 'a '}list of nonempty strings")
        return set(values)

    @classmethod
    def load(cls, filename: str, repo: str) -> QueuePolicy:
        return cls(json.loads(Path(filename).read_text(encoding="utf-8")), repo)

    def allows(self, run: dict[str, Any]) -> bool:
        if not isinstance(run, dict):
            return False
        if not isinstance(run.get("event"), str) or not isinstance(run.get("path"), str):
            return False
        workflow_path = run["path"].split("@", 1)[0]
        if run["event"] not in self.events or workflow_path not in self.paths:
            return False
        repository = run.get("repository")
        if not isinstance(repository, dict) or str(repository.get("full_name", "")).lower() != self.repo:
            return False
        event = run.get("event")
        head_repo = run.get("head_repository")
        if event == "push" or (event == "pull_request" and self.same_repo_pr):
            if not isinstance(head_repo, dict) or str(head_repo.get("full_name", "")).lower() != self.repo:
                return False
        if event == "push" and self.push_branches and run.get("head_branch") not in self.push_branches:
            return False
        if event == "pull_request" and self.same_repo_pr:
            repo_id = repository.get("id")
            prs = run.get("pull_requests")
            if type(repo_id) is not int or repo_id <= 0 or not isinstance(prs, list) or not prs:
                return False
            if type(head_repo.get("id")) is not int or head_repo["id"] != repo_id:
                return False
            for pr in prs:
                if not isinstance(pr, dict):
                    return False
                for side in ("head", "base"):
                    value = pr.get(side)
                    value = value.get("repo") if isinstance(value, dict) else None
                    if not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] != repo_id:
                        return False
        return True
