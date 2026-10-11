"""Fair host-global observation lock with per-repository timeout backoff."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import time
import datetime as dt
from pathlib import Path
from typing import Iterator

from bounded_subprocess import ObservationError


class ObservationBackoff(RuntimeError):
    """This repository recently timed out and must yield the host slot."""


class ObservationTimeout(TimeoutError):
    """A scanner exhausted a bounded observation timeout."""


def _process_start(pid: int) -> str | None:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "lstart="], text=True,
            stderr=subprocess.DEVNULL,
        ).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _timed_out(error: BaseException) -> bool:
    """Recognize typed observation timeouts through scanner wrapping layers."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ObservationError) and current.kind == "timeout":
            return True
        if isinstance(current, TimeoutError):
            return True
        current = current.__cause__ or current.__context__
    return False


class FairObservationLock:
    def __init__(self, lock_file: str, timeout: float, repo: str,
                 backoff_file: str | None = None, backoff_seconds: float = 30.0,
                 lane: str | None = None, generation: str | None = None,
                 max_age: float | None = None) -> None:
        self.lock_path = Path(lock_file)
        self.timeout = timeout
        self.repo = repo
        self.backoff_path = Path(backoff_file or (str(self.lock_path) + ".backoff.json"))
        self.backoff_seconds = backoff_seconds
        self.lane = lane or os.environ.get("TARTCI_QUEUE_LANE_ID") or f"pid-{os.getppid()}"
        self.generation = generation or os.environ.get("TARTCI_GENERATION") or "unknown"
        self.max_age = max_age if max_age is not None else float(
            os.environ.get("TARTCI_QUEUE_OBSERVATION_LOCK_MAX_AGE_SECS", "300")
        )
        self.owner_path = self.lock_path.with_name(self.lock_path.name + ".owner.json")
        self.escalation_path = self.lock_path.with_name(
            self.lock_path.name + ".escalation.json"
        )
        self.backoff_lock_path = self.backoff_path.with_name(self.backoff_path.name + ".lock")
        self.ticket_path: Path | None = None
        self.handle = None

    def _backoff(self) -> dict[str, float]:
        try:
            value = json.loads(self.backoff_path.read_text())
            return {str(k): float(v) for k, v in value.items()}
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {}

    def _write_backoff(self, values: dict[str, float]) -> None:
        self.backoff_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.backoff_path.with_name(
            self.backoff_path.name + f".{os.getpid()}.{time.time_ns()}.tmp"
        )
        temp.write_text(json.dumps(values, sort_keys=True, separators=(",", ":")))
        os.replace(temp, self.backoff_path)

    def _check_backoff(self) -> None:
        until = self._backoff().get(self.repo, 0.0)
        if until > time.time():
            raise ObservationBackoff(
                f"host queue observation lock backoff active for {until - time.time():.1f}s"
            )

    def _mark_timeout(self) -> None:
        with self.backoff_lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            values = self._backoff()
            values[self.repo] = time.time() + self.backoff_seconds
            self._write_backoff(values)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _clear_backoff(self) -> None:
        with self.backoff_lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            values = self._backoff()
            if self.repo in values:
                values.pop(self.repo, None)
                self._write_backoff(values)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _owner(self) -> dict[str, object] | None:
        try:
            value = json.loads(self.owner_path.read_text())
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _owner_detail(self) -> str:
        owner = self._owner()
        if not owner:
            return "owner=unknown age=unknown"
        started = owner.get("started_at")
        try:
            age = max(0.0, time.time() - float(owner["started_epoch"]))
            age_text = f"{age:.1f}s"
        except (KeyError, TypeError, ValueError):
            age_text = "unknown"
        detail = (
            f"owner_pid={owner.get('pid', 'unknown')} "
            f"lane={owner.get('lane', 'unknown')} "
            f"generation={owner.get('generation', 'unknown')} "
            f"started_at={started or 'unknown'} age={age_text}"
        )
        if age_text != "unknown" and float(age_text[:-1]) >= self.max_age:
            escalation = {
                **owner,
                "age_seconds": float(age_text[:-1]),
                "escalated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "reason": "observation lock owner exceeded bounded age",
            }
            temp = self.escalation_path.with_name(
                self.escalation_path.name + f".{os.getpid()}.{time.time_ns()}.tmp"
            )
            try:
                temp.write_text(json.dumps(escalation, sort_keys=True, separators=(",", ":")))
                os.replace(temp, self.escalation_path)
            except OSError:
                try:
                    temp.unlink()
                except OSError:
                    pass
            detail += f" escalation=owner_age_exceeded:{self.max_age:.1f}s"
        return detail

    def _next_ticket(self, queue: Path) -> Path:
        queue.mkdir(parents=True, exist_ok=True)
        counter = queue / "counter"
        with counter.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            raw = handle.read().strip()
            ticket = int(raw or "0") + 1
            handle.seek(0)
            handle.truncate()
            handle.write(str(ticket))
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        marker = queue / f"{ticket:020d}.{os.getpid()}"
        marker.write_text(json.dumps({"pid": os.getpid(), "start": _process_start(os.getpid())}))
        return marker

    @staticmethod
    def _remove_stale(queue: Path) -> None:
        for marker in queue.glob("[0-9]*.*"):
            try:
                owner = json.loads(marker.read_text())
                pid = int(owner["pid"])
                if not owner.get("start") or owner["start"] != _process_start(pid):
                    raise ProcessLookupError(pid)
                os.kill(pid, 0)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError,
                    FileNotFoundError, ProcessLookupError, PermissionError):
                try:
                    marker.unlink()
                except FileNotFoundError:
                    pass

    @contextlib.contextmanager
    def hold(self) -> Iterator[object]:
        self._check_backoff()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        queue = self.lock_path.with_name(self.lock_path.name + ".fifo")
        marker = self._next_ticket(queue)
        self.ticket_path = marker
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                self._remove_stale(queue)
                lower = sorted(p for p in queue.glob("[0-9]*.*") if p.name < marker.name)
                if not lower:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "host queue observation lock timed out; "
                        + self._owner_detail()
                    )
                time.sleep(0.05)
            with self.lock_path.open("a+", encoding="utf-8") as handle:
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                "host queue observation lock timed out; "
                                + self._owner_detail()
                            )
                        time.sleep(0.05)
                self.handle = handle
                owner = {
                    "pid": os.getpid(),
                    "lane": self.lane,
                    "generation": self.generation,
                    "started_epoch": time.time(),
                    "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
                temp = self.owner_path.with_name(
                    self.owner_path.name + f".{os.getpid()}.{time.time_ns()}.tmp"
                )
                temp.write_text(json.dumps(owner, sort_keys=True, separators=(",", ":")))
                os.replace(temp, self.owner_path)
                try:
                    yield handle
                except BaseException as error:
                    if _timed_out(error):
                        self._mark_timeout()
                    raise
                else:
                    self._clear_backoff()
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            if marker.exists():
                marker.unlink()
            self.ticket_path = None
