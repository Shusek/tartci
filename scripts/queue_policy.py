"""Optional repository-bound admission policy for Actions queue discovery.

This controls VM demand, not GitHub's eventual runner-to-job assignment.
Runner groups and workflow labels must enforce the same routing boundaries.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any


class QueuePolicy:
    APPROVAL_TTL = 24 * 60 * 60
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

    def approval(self, run: dict[str, Any], now: int) -> dict[str, Any] | None:
        """Keep only public authorization facts proven before PR metadata disappears."""
        if not self.same_repo_pr or run.get('event') != 'pull_request' or not self.allows(run):
            return None
        if any(type(run.get(k)) is not int or run[k] <= 0 for k in ('id', 'run_attempt', 'workflow_id')):
            return None
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', str(run.get('head_sha', ''))):
            return None
        if any(not isinstance(run.get(k), str) or not run[k] for k in ('name', 'head_branch')):
            return None
        fields = ('id', 'run_attempt', 'workflow_id', 'name', 'event', 'path', 'head_sha', 'head_branch')
        saved = {k: run[k] for k in fields}
        for key in ('repository', 'head_repository'):
            saved[key] = {k: run[key][k] for k in ('id', 'full_name')}
        saved['pull_requests'] = [
            {side: {'repo': {'id': pr[side]['repo']['id']}} for side in ('head', 'base')}
            for pr in run['pull_requests']
        ]
        return {'approved_at': now, 'policy': self.fingerprint, 'run': saved}

    def allows_approved(self, run: dict[str, Any], proof: Any, now: int) -> bool:
        # Missing evidence alone is eligible for fallback. Conflicting/fork
        # evidence, another attempt/head/workflow or a policy change never is.
        if run.get('pull_requests') != [] or not isinstance(proof, dict):
            return False
        stamp, original = proof.get('approved_at'), proof.get('run')
        if (type(stamp) is not int or not 0 <= now - stamp <= self.APPROVAL_TTL
                or proof.get('policy') != self.fingerprint or not isinstance(original, dict)
                or self.approval(original, stamp) != proof):
            return False
        for key in ('id', 'run_attempt', 'workflow_id', 'name', 'event', 'path', 'head_sha', 'head_branch'):
            if run.get(key) != original.get(key):
                return False
        for key in ('repository', 'head_repository'):
            live = run.get(key)
            if not isinstance(live, dict) or any(live.get(k) != original[key][k] for k in ('id', 'full_name')):
                return False
        return True

    def remember(self, run: dict[str, Any], directory: str | None, now: int) -> bool:
        """Called under the host observation lock, using fresh GitHub evidence."""
        if not directory:
            return False
        proof = self.approval(run, now)
        if proof is None:
            return False
        root = Path(directory)
        leaf = root / self.fingerprint
        for path in (root, leaf):
            if path.is_symlink():
                raise ValueError('approval directory is a symlink')
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = path.stat()
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError('approval directory is not private to the observer')
        with tempfile.NamedTemporaryFile('w', dir=leaf, delete=False) as stream:
            json.dump(proof, stream)
            temporary = Path(stream.name)
        try:
            os.replace(temporary, leaf / (str(run['id']) + '.json'))
        finally:
            temporary.unlink(missing_ok=True)
        return True

    def prune_approvals(self, directory: str, now: int) -> None:
        # Bounded public metadata, never a credential or an authorization for
        # another head/attempt. Missing/expired records always fail closed.
        leaf = Path(directory) / self.fingerprint
        info = leaf.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('approval directory is not private to the observer')
        entries = sorted(leaf.glob('[0-9]*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for index, path in enumerate(entries):
            if index >= 1024 or now - path.stat().st_mtime > self.APPROVAL_TTL:
                path.unlink()

    def approved(self, run: dict[str, Any], directory: str | None, now: int) -> bool:
        if not directory or type(run.get('id')) is not int or run['id'] <= 0:
            return False
        try:
            root = Path(directory)
            leaf = root / self.fingerprint
            for path in (root, leaf):
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    return False
            fd = os.open(leaf / (str(run['id']) + '.json'), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'r') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 65536:
                    return False
                proof = json.load(stream)
            return self.allows_approved(run, proof, now)
        except (OSError, ValueError, TypeError):
            return False
