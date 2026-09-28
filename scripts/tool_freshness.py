#!/usr/bin/env python3
"""Installed-vs-released freshness for the host tools a fleet host runs.

tartci measures its own skew against main (`fleet_self_update.py`). The other
tools every gate and agent session on the host depends on, Shipyard and the
pulp CLI, had no equivalent, and an installed pulp CLI sat ~2,800 hours behind
its releases before anyone looked. This module gives each of them one line in
`tartci pool status`, a finding in `tartci doctor fleet`, and a WARN in the
launchd watchdog, and optionally applies the update itself:

  refresh   (the watchdog, at most every 30 minutes) reads the installed
            version by running the tool, and the published releases from
            the repository's public Atom feed (no REST quota). A tool whose
            installed version is older than the newest release is `behind`
            since that newer release was published; it is STALE once that is
            more than `stale_hours` ago. When a tool has an `apply_command`
            and `auto_apply`, a behind tool is updated once its newest
            release is `apply_after_minutes` old, at most once per target per
            `apply_retry_hours`, and the result is re-read and verified.
  events    every change of an installed version, whoever made it, appends
            one `tool_deployed` event (tool, from, to, verify) to
            events.jsonl; an automatic apply that did not land appends
            `tool_apply_failed`.
  summary   reads only the cached state; it never runs a tool or fetches.

Settings (optional) live in ~/.config/tartci/tool-freshness.toml:

    stale_hours = 12
    [tools.shipyard]
    auto_apply = false            # stop automatic Shipyard updates here
    [tools.pulp]
    enabled = false               # stop measuring the pulp CLI here

`{home}` and `{tag}` are substituted in commands. A tool whose binary is
absent is `not_installed`, which is reported but is never a problem.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - the launchd python is 3.9
    tomllib = None  # type: ignore[assignment]

STALE_HOURS = 12.0
APPLY_AFTER_MINUTES = 30.0
APPLY_RETRY_HOURS = 6.0
REFRESH_INTERVAL_S = 1800
COMMAND_TIMEOUT_S = 30
APPLY_TIMEOUT_S = 300
FEED_TIMEOUT_S = 15

DEFAULT_TOOLS: dict[str, dict[str, Any]] = {
    "shipyard": {
        "repo": "danielraffel/Shipyard",
        "version_command": ["{home}/.local/bin/shipyard", "--version"],
        # Installs the tag, smoke-verifies it, and only then refreshes the
        # detached daemon: Shipyard's own unattended fleet rollout path.
        "apply_command": ["{home}/.local/bin/shipyard", "update", "--to", "{tag}",
                          "--refresh-daemon"],
        "auto_apply": True,
    },
    "pulp": {
        "repo": "Generous-Corp/pulp",
        "version_command": ["{home}/.pulp/bin/pulp", "version"],
        # The pulp CLI updates itself at session start; measured here only.
        "apply_command": None,
        "auto_apply": False,
    },
}

ATOM = "{http://www.w3.org/2005/Atom}"
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
TAG_RE = re.compile(r"/releases/tag/(v?\d+\.\d+\.\d+)$")


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return dt.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def _version(text: str | None) -> tuple[int, int, int] | None:
    match = VERSION_RE.search(text or "")
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def _vstr(version: tuple[int, int, int] | None) -> str | None:
    return ".".join(str(part) for part in version) if version else None


def state_dir_for(home: Path) -> Path:
    root = os.environ.get("TARTCI_HOME") or str(home / ".tartci")
    return Path(root) / "state" / "tool-freshness"


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def _append_event(state: Path, event: dict) -> None:
    state.mkdir(parents=True, exist_ok=True)
    with (state / "events.jsonl").open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def load_settings(home: Path, path: Path | None = None) -> dict[str, Any]:
    """Defaults merged with ~/.config/tartci/tool-freshness.toml when present."""
    settings: dict[str, Any] = {
        "stale_hours": STALE_HOURS, "apply_after_minutes": APPLY_AFTER_MINUTES,
        "apply_retry_hours": APPLY_RETRY_HOURS,
        "tools": {name: dict(tool, enabled=True) for name, tool in DEFAULT_TOOLS.items()},
    }
    path = path or home / ".config" / "tartci" / "tool-freshness.toml"
    if path.is_file() and tomllib is not None:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        for key in ("stale_hours", "apply_after_minutes", "apply_retry_hours"):
            if key in data:
                settings[key] = float(data[key])
        for name, override in (data.get("tools") or {}).items():
            settings["tools"].setdefault(name, {"enabled": True, "apply_command": None,
                                                "auto_apply": False})
            settings["tools"][name].update(override)
    return settings


def _expand(argv: list[str] | None, home: Path, tag: str = "") -> list[str] | None:
    if not argv:
        return None
    return [part.replace("{home}", str(home)).replace("{tag}", tag) for part in argv]


def run_command(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "not installed"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def fetch_feed(repo: str) -> str:
    request = urllib.request.Request(f"https://github.com/{repo}/releases.atom",
                                     headers={"User-Agent": "tartci-tool-freshness"})
    with urllib.request.urlopen(request, timeout=FEED_TIMEOUT_S) as response:  # noqa: S310
        return response.read().decode("utf-8", "replace")


def parse_releases(feed: str) -> list[tuple[tuple[int, int, int], str, float]]:
    """(version, tag, published epoch) for every plain vX.Y.Z release in a feed.

    A repository that publishes more than one release family (the pulp repo
    also tags plugin-vX.Y.Z) keeps only the plain one.
    """
    releases = []
    for entry in ET.fromstring(feed).iter(f"{ATOM}entry"):
        link = entry.find(f"{ATOM}link")
        match = TAG_RE.search(link.get("href", "") if link is not None else "")
        updated = _epoch((entry.findtext(f"{ATOM}updated") or "").strip())
        version = _version(match.group(1)) if match else None
        if version and updated is not None:
            releases.append((version, match.group(1), updated))  # type: ignore[union-attr]
    return sorted(releases)


def measure(name: str, tool: dict, home: Path, now: float, previous: dict | None,
            run: Callable[[list[str], float], tuple[int, str]],
            feed: Callable[[str], str], stale_hours: float) -> dict:
    row: dict[str, Any] = {"tool": name, "repo": tool.get("repo"), "measured_at": _iso(now)}
    argv = _expand(tool.get("version_command"), home)
    rc, out = run(argv, COMMAND_TIMEOUT_S) if argv else (127, "no version_command")
    installed = _version(out) if rc == 0 else None
    row["installed"] = _vstr(installed)
    if rc == 127:
        row.update(state="not_installed", reason=out)
        return row
    if installed is None:
        row.update(state="unknown", reason=f"version unreadable (exit {rc}): {out[:200]}")
        return row
    try:
        releases = parse_releases(feed(str(tool["repo"])))
    except Exception as exc:  # noqa: BLE001 - an unreadable feed is unknown, never current
        row.update(state="unknown", reason=f"releases unreadable: {type(exc).__name__}: {exc}")
        return row
    if not releases:
        row.update(state="unknown", reason="no vX.Y.Z release in the feed")
        return row
    latest, latest_tag, latest_at = releases[-1]
    row.update(latest=_vstr(latest), latest_tag=latest_tag, latest_published_at=_iso(latest_at))
    newer = [release for release in releases if release[0] > installed]
    if not newer:
        row["state"] = "current"
        return row
    behind_since = newer[0][2]
    # The feed holds only the newest releases, so an install older than all of
    # them is behind since at least the oldest one listed. Keep the earliest
    # time recorded for this installed version, so the bound never moves later.
    lower_bound = len(newer) == len(releases)
    if previous and previous.get("installed") == row["installed"]:
        earlier = _epoch(previous.get("behind_since"))
        if earlier is not None and earlier < behind_since:
            behind_since = earlier
            lower_bound = bool(previous.get("behind_since_lower_bound"))
    hours = max(0.0, (now - behind_since) / 3600)
    row.update(state="behind", releases_behind=len(newer), behind_since=_iso(behind_since),
               behind_since_lower_bound=lower_bound, behind_hours=round(hours, 1),
               stale=hours > stale_hours)
    return row


def maybe_apply(name: str, tool: dict, row: dict, home: Path, now: float, state: Path,
                settings: dict, attempts: dict,
                run: Callable[[list[str], float], tuple[int, str]]) -> dict | None:
    """Apply a behind tool's update when allowed; return the re-measured version."""
    if row.get("state") != "behind" or not tool.get("auto_apply") or not tool.get("apply_command"):
        return None
    tag = str(row["latest_tag"])
    published = _epoch(row.get("latest_published_at")) or now
    if now - published < float(settings["apply_after_minutes"]) * 60:
        row["apply"] = f"waiting: {tag} is younger than {settings['apply_after_minutes']:g} min"
        return None
    last = attempts.get(name) or {}
    if last.get("target") == tag and now - (_epoch(last.get("at")) or 0) < \
            float(settings["apply_retry_hours"]) * 3600:
        row["apply"] = f"already attempted {tag} at {last.get('at')}: {last.get('result')}"
        return None
    argv = _expand(tool["apply_command"], home, tag)
    rc, out = run(argv, APPLY_TIMEOUT_S)  # type: ignore[arg-type]
    vrc, vout = run(_expand(tool["version_command"], home), COMMAND_TIMEOUT_S)  # type: ignore[arg-type]
    after = _vstr(_version(vout)) if vrc == 0 else None
    landed = after == row["latest"]
    result = "ok" if landed else f"FAILED (exit {rc}, installed {after}): {out[-300:]}"
    attempts[name] = {"target": tag, "at": _iso(now), "result": result}
    row["apply"] = f"applied {tag}: {result}"
    if not landed:
        _append_event(state, {"event": "tool_apply_failed", "tool": name, "at": _iso(now),
                              "from": row["installed"], "target": row["latest"],
                              "installed_after": after, "exit": rc, "detail": out[-300:]})
    return {"installed": after}


def refresh(home: Path, now: float | None = None, *, if_older: int = 0,
            settings: dict | None = None,
            run: Callable[[list[str], float], tuple[int, str]] = run_command,
            feed: Callable[[str], str] = fetch_feed) -> dict:
    """Measure every enabled tool, apply where allowed, record deployments."""
    now = time.time() if now is None else now
    settings = settings or load_settings(home)
    state = state_dir_for(home)
    cached = _read_json(state / "state.json") or {}
    if if_older and cached.get("measured_at"):
        age = now - (_epoch(cached["measured_at"]) or 0)
        if age < if_older:
            return cached
    previous = cached.get("tools") or {}
    attempts = _read_json(state / "attempts.json") or {}
    tools: dict[str, dict] = {}
    for name, tool in settings["tools"].items():
        if not tool.get("enabled", True):
            continue
        prior = previous.get(name) if isinstance(previous.get(name), dict) else None
        row = measure(name, tool, home, now, prior, run, feed, float(settings["stale_hours"]))
        before = prior.get("installed") if prior else None
        applied = maybe_apply(name, tool, row, home, now, state, settings, attempts, run)
        if applied and applied.get("installed"):
            before, note = row.get("installed"), row.get("apply")
            row = measure(name, tool, home, now, None, run, feed, float(settings["stale_hours"]))
            row["apply"] = note
        if before and row.get("installed") and before != row["installed"]:
            _append_event(state, {
                "event": "tool_deployed", "tool": name, "at": _iso(now),
                "from": before, "to": row["installed"], "latest": row.get("latest"),
                "by": "auto_apply" if applied else "observed",
                "downgrade": _version(row["installed"]) < _version(before),  # type: ignore[operator]
                "verify": "current" if row.get("state") == "current"
                else str(row.get("state"))})
        tools[name] = row
    value = {"measured_at": _iso(now), "stale_hours": settings["stale_hours"], "tools": tools}
    _write_json(state / "state.json", value)
    _write_json(state / "attempts.json", attempts)
    return value


def render_row(row: dict) -> str:
    name, state = row.get("tool"), row.get("state")
    if state == "current":
        text = f"{name}: {row['installed']} current with latest {row['latest_tag']}"
    elif state == "behind":
        bound = ">=" if row.get("behind_since_lower_bound") else ""
        text = (f"{name}: {row['installed']} behind latest {row['latest_tag']} by "
                f"{row['releases_behind']} release(s) for {bound}{row['behind_hours']:g} h "
                f"(since {row['behind_since']})" + (" STALE" if row.get("stale") else ""))
    elif state == "not_installed":
        text = f"{name}: not installed"
    else:
        text = f"{name}: freshness UNKNOWN ({row.get('reason')})"
    if row.get("apply"):
        text += f" [{row['apply']}]"
    return text + f" (measured {row.get('measured_at')})"


def summary(home: Path | None = None) -> dict:
    """Cached freshness for status surfaces. Never runs a tool or fetches."""
    value = _read_json(state_dir_for(home or Path.home()) / "state.json")
    if value is None:
        return {"state": None, "lines": ["tools: freshness UNKNOWN (never measured; run "
                                         "tartci fleet-macos tool-freshness --refresh)"],
                "problem": None}
    rows = [row for row in (value.get("tools") or {}).values() if isinstance(row, dict)]
    problems = [f"{row['tool']} {row['behind_hours']:g} h behind {row['latest_tag']}"
                for row in rows if row.get("stale")]
    problems += [f"{row['tool']} freshness unknown" for row in rows
                 if row.get("state") == "unknown"]
    return {"state": value, "lines": [render_row(row) for row in rows],
            "problem": "; ".join(problems) or None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci fleet-macos tool-freshness")
    parser.add_argument("--refresh", action="store_true",
                        help="measure now (and apply where auto_apply allows)")
    parser.add_argument("--if-older", type=int, default=0,
                        help="with --refresh: skip unless the cache is older (seconds)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--home", default=str(Path.home()))
    args = parser.parse_args(argv)
    home = Path(args.home)
    if args.refresh:
        refresh(home, if_older=args.if_older)
    value = summary(home)
    if args.json:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print("\n".join(value["lines"]))
    return 1 if value["problem"] else 0


if __name__ == "__main__":
    sys.exit(main())
