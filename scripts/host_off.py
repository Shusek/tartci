#!/usr/bin/env python3
"""A host that a failed self-update left OFF: detect it, recover it, say so.

On m3 on 2026-09-29 two self-updates in a row installed, could not `pool on`
(the signed launch helper's volume probe timed out on the Workshop volume),
rolled back, could not `pool on` again, and left the host OFF. It stayed OFF
from 08:40Z to 17:16Z. The next scheduled runs were refused by the 6 h
same-target guard before they looked at the pool at all, and the only signal
was a WARN line in the watchdog log. The cause was the volume, not the code:
the rollback failed identically, and the first run after the volume recovered
succeeded.

Three pieces, all reading the self-update agent's own records:

  status()   Is this host OFF because a self-update left it that way, and not
             because someone ran `pool off` since? `last.json` says the update
             left the host off; the pool-state file's mtime says whether the
             pool was changed after that. Unexpected OFF past
             LOUD_AFTER_SECONDS (15 min) is `loud`.
  recover()  Try `pool on` for the generation that is installed now, no more
             often than RECOVERY_BACKOFF_SECONDS. It changes no code and
             installs nothing, so the same-target guard does not apply to it.
             On success `last.json` records the recovery.
  alert()    Once per episode: a `host_off_unexpected` event and, best effort,
             a GitHub issue; on recovery a `host_off_recovered` event and the
             issue is closed.

The self-update run (before any update logic) and the launchd watchdog's
5-minute heal pass both call recover(); `pool status` and `doctor fleet`
show status() as problem `host_off_unexpected`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

LOUD_AFTER_SECONDS = 15 * 60
RECOVERY_BACKOFF_SECONDS = 5 * 60
# `pool on` after `last.json` moves the pool-state file too; a change later
# than this after the update finished is a person's decision.
DELIBERATE_SLACK_SECONDS = 120
ISSUE_REPO = "danielraffel/tartci"


def state_dir(home: Path | None = None) -> Path:
    home = home or Path.home()
    root = os.environ.get("TARTCI_HOME") or str(home / ".tartci")
    return Path(root) / "state" / "self-update"


def pool_state_file(home: Path | None = None) -> Path:
    home = home or Path.home()
    return Path(os.environ.get("TARTCI_POOL_STATE_FILE")
                or str(home / ".config" / "tartci" / "pool-state"))


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _ts(text: Any) -> float | None:
    try:
        return dt.datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_pool_state(path: Path) -> str:
    try:
        value = path.read_text().strip()
    except OSError:
        return "unknown"
    return value if value in ("on", "draining", "off") else "unknown"


def status(sdir: Path, pool_file: Path, now: float | None = None,
           pool_state: str | None = None) -> dict:
    """Whether a self-update left this host OFF and nobody has touched it since."""
    now = time.time() if now is None else now
    last = _read_json(sdir / "last.json") or {}
    pool_state = pool_state or read_pool_state(pool_file)
    out: dict[str, Any] = {"unexpected": False, "loud": False, "pool_state": pool_state}
    if not last.get("host_off"):
        return out
    since = _ts(last.get("at"))
    if since is None:
        return out
    try:
        changed = pool_file.stat().st_mtime
    except OSError:
        changed = None
    if pool_state == "on":
        out["reason"] = "pool is on again"
        return out
    # A failed `pool on` of our own can rewrite the pool-state file; only a
    # change after the last write we made (or the update's own) is a person's.
    baseline = since
    record = _read_json(sdir / "recovery.json") or {}
    if record.get("since") == _iso(since) and isinstance(record.get("pool_mtime"), (int, float)):
        baseline = max(baseline, float(record["pool_mtime"]))
    if changed is not None and changed > baseline + DELIBERATE_SLACK_SECONDS:
        out["reason"] = f"pool set {pool_state} deliberately at {_iso(changed)}"
        return out
    seconds = max(0.0, now - since)
    out.update(unexpected=True, loud=seconds >= LOUD_AFTER_SECONDS, since=_iso(since),
               minutes=int(seconds // 60),
               cause=str(last.get("error") or "")[:300],
               target=str(last.get("target") or "")[:12],
               detail=(f"a failed self-update left this host {pool_state.upper()} for "
                       f"{int(seconds // 60)} min (since {_iso(since)}): "
                       f"{str(last.get('error') or '')[:200]}"))
    return out


def event(sdir: Path, name: str, detail: str, fields: dict | None = None,
          now: float | None = None) -> None:
    """Append one event to the self-update event log. Never raises."""
    row = {"ts": _iso(time.time() if now is None else now), "event": name, "detail": detail}
    if fields:
        row["fields"] = fields
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        with (sdir / "events.jsonl").open("a") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass


def _active_update(sdir: Path) -> bool:
    """A self-update is running right now (its marker names a live pid)."""
    marker = _read_json(sdir / "active.json")
    if not marker:
        return False
    try:
        os.kill(int(marker.get("pid")), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def recover(sdir: Path, pool_file: Path, pool_on: Callable[[], tuple[int, str]],
            now: float | None = None, backoff: float = RECOVERY_BACKOFF_SECONDS,
            who: str = "", pool_state: str | None = None) -> dict:
    """Try `pool on` for the installed generation when the host was left OFF.

    Returns {"attempted": bool, "ok": bool | None, "reason": str}.
    """
    now = time.time() if now is None else now
    current = status(sdir, pool_file, now, pool_state=pool_state)
    if not current["unexpected"]:
        return {"attempted": False, "ok": None, "reason": current.get("reason") or "not left off"}
    if _active_update(sdir):
        return {"attempted": False, "ok": None, "reason": "a self-update is running"}
    record = _read_json(sdir / "recovery.json") or {}
    last_try = _ts(record.get("attempted_at"))
    if last_try is not None and record.get("since") == current["since"] \
            and now - last_try < backoff:
        return {"attempted": False, "ok": None,
                "reason": f"backing off: last recovery attempt {int(now - last_try)}s ago"}
    rc, text = pool_on()
    ok = rc == 0
    attempts = int(record.get("attempts", 0)) + 1 if record.get("since") == current["since"] else 1
    try:
        pool_mtime: float | None = pool_file.stat().st_mtime
    except OSError:
        pool_mtime = None
    _write_json(sdir / "recovery.json", {
        "since": current["since"], "attempted_at": _iso(now), "attempts": attempts,
        "ok": ok, "rc": rc, "detail": text[:400], "by": who, "pool_mtime": pool_mtime})
    if ok:
        last = _read_json(sdir / "last.json") or {}
        last.update(host_off=False, pool_state="on", recovered_at=_iso(now),
                    recovered_by=who or "recovery")
        _write_json(sdir / "last.json", last)
        event(sdir, "host_off_recovered",
              f"pool on succeeded after {current['minutes']} min OFF ({attempts} attempt(s), by {who})",
              {"since": current["since"], "minutes": current["minutes"], "attempts": attempts},
              now)
    else:
        event(sdir, "host_off_recovery_failed",
              f"pool on exit {rc} after {current['minutes']} min OFF: {text[:200]}",
              {"since": current["since"], "minutes": current["minutes"], "rc": rc}, now)
    return {"attempted": True, "ok": ok, "reason": text[:200], "attempts": attempts}


def _ghapp(args: list[str], cwd: str | None) -> tuple[int, str]:
    try:
        proc = subprocess.run(["ghapp", *args], cwd=cwd, capture_output=True, text=True,
                              timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def alert(sdir: Path, pool_file: Path, host: str, now: float | None = None,
          issue: Callable[[str, str], tuple[int, str]] | None = None,
          close: Callable[[str], tuple[int, str]] | None = None) -> dict:
    """Once per episode: event + GitHub issue when loud; close it on recovery.

    `issue(title, body)` and `close(number)` default to ghapp; a failure is
    recorded in the alert state and retried on the next pass.
    """
    now = time.time() if now is None else now
    current = status(sdir, pool_file, now)
    path = sdir / "host-off-alert.json"
    state = _read_json(path) or {}
    out = {"loud": current["loud"], "evented": False, "issue": state.get("issue")}
    if current["loud"]:
        if state.get("since") != current["since"]:
            state = {"since": current["since"]}
        if not state.get("evented"):
            event(sdir, "host_off_unexpected", current["detail"],
                  {"since": current["since"], "minutes": current["minutes"],
                   "target": current.get("target")}, now)
            state["evented"] = True
            out["evented"] = True
        if not state.get("issue") and os.environ.get("TARTCI_HOST_OFF_ISSUE", "1") != "0":
            title = f"[tartci] {host} left OFF by a failed self-update since {current['since']}"
            body = (f"{current['detail']}\n\nThe host's launchd watchdog and self-update agent "
                    "retry `pool on` every 5 min; this issue closes itself when the pool is on "
                    "again. Check `tartci pool status` and "
                    "`~/.tartci/state/self-update/last.json` on the host.")
            rc, text = (issue or _open_issue)(title, body)
            if rc == 0 and text.strip().isdigit():
                state["issue"] = text.strip()
                out["issue"] = state["issue"]
            else:
                state["issue_error"] = text[:300]
        _write_json(path, state)
    elif state.get("since") and not current["unexpected"]:
        if state.get("issue"):
            (close or _close_issue)(str(state["issue"]))
        path.unlink(missing_ok=True)
    return out


def _update_checkout() -> str | None:
    checkout = Path.home() / ".local" / "share" / "tartci" / "update-checkout"
    return str(checkout) if (checkout / ".git").exists() else None


def _open_issue(title: str, body: str) -> tuple[int, str]:
    # ghapp derives repository provenance from the working directory, so it
    # runs inside the tartci checkout the self-update agent keeps.
    return _ghapp(["api", "-X", "POST", f"repos/{ISSUE_REPO}/issues", "-f", f"title={title}",
                   "-f", f"body={body}", "--jq", ".number"], _update_checkout())


def _close_issue(number: str) -> tuple[int, str]:
    return _ghapp(["api", "-X", "PATCH", f"repos/{ISSUE_REPO}/issues/{number}",
                   "-f", "state=closed", "--jq", ".state"], _update_checkout())
