#!/usr/bin/env python3
"""Stop cloning VMs on a host whose VM DHCP server has stopped answering.

A booted VM gets its address from the host's macOS DHCP server (bootpd, a
socket-activated system daemon that Internet Sharing manages). On m5 it
stopped answering twice (2026-09-23 10:16-11:03Z, 13 VMs; 2026-10-04
21:05-21:45Z, 10 VMs). Every lane kept cloning, waiting 120 s for an address
(`boot_failed no_ip`) and discarding, while no boot could succeed: on 10-04
bootpd ran nothing from 20:53Z until smd re-enabled it at 21:48:51Z. Recovery
needs root, so tartci never attempts it; it stops spending VMs until an
address comes back, and says so.

One breaker per host, in BREAKER_DIR/breaker.json, shared by every lane:

* closed: lanes clone as usual. Each `no_ip` is recorded; K (2) of them
  within N (15 min) with no address in between opens the breaker. A single
  `no_ip` never has: both isolated ones on record cleared on the next boot.
* open: no lane clones (`check` answers `backoff`, an idle pass). Once per
  PROBE_SECS (300), at once when the VM-network chain has changed since the
  last look (bootpd loaded or its run count, /etc/bootpd.plist's mtime, or
  InternetSharing's pid: an operator acted), or at once after `probe-now`,
  exactly one lane is answered `probe` and clones one VM. Every probe is
  recorded `vm_dhcp_probe result=ip|no_ip`.
* closed again: the first address any VM on the host gets, probe or not
  (`record ip`), closes it and reports how long it was open, how many VMs it
  spent, and the recovery latency. A breaker opened before the host last
  booted is closed too (`reason=host_reboot`): a reboot resets bootpd.

The trade is explicit: an outage now costs about one VM per PROBE_SECS
instead of one per lane every 2-4 min, and recovery is noticed within
PROBE_SECS plus a boot instead of within minutes.

Fail open: an unreadable or corrupt breaker reads as closed, so a lane never
refuses to boot over state it cannot read. Writes are atomic (tmp + rename)
under an exclusive lock, so a reader never sees a partial file.

Python 3.9-safe: every lane calls it with a bare `python3`, and a host whose
lane PATH falls through to /usr/bin/python3 must still stop cloning.

Every `no_ip`, read while that VM is still up, also records which layer
failed (`cause`), most fundamental first:
  vm_network_missing  no bridge100 exists: InternetSharing (vmnet shared
                      mode) never created the VM network, so bootpd has
                      nothing to serve (m5, 2026-10-07)
  bootpd_not_loaded   launchd has no bootpd job (`launchctl print` exit 113)
  dhcp_config_disabled  the network exists but /etc/bootpd.plist does not
                      enable DHCP on it
  dhcp_silent         everything is in place and bootpd still answers nothing
/etc/bootpd.plist saying dhcp_enabled=false is the NORMAL idle state: Internet
Sharing rewrites it when it creates bridge100, so it only means something
while a VM is up.

Usage: vm_dhcp_breaker.py check --lane L | record --outcome ip|no_ip --lane L
[--vm V] | probe-now | status --json. `check` and `record` print {"action", "events"}; the
caller emits the events.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - tartci hosts are POSIX.
    fcntl = None  # type: ignore[assignment]

K = 2
WINDOW_S = 15 * 60
PROBE_SECS = 300
MAX_STREAK = 50


def breaker_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get(
        "TARTCI_VM_DHCP_DIR", str(pathlib.Path.home() / ".tartci" / "state" / "vm-dhcp")))


def setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


@contextlib.contextmanager
def locked(directory: pathlib.Path) -> Iterator[pathlib.Path]:
    directory.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        raise OSError("breaker requires fcntl")
    with (directory / "breaker.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield directory / "breaker.json"
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load(path: pathlib.Path) -> dict[str, Any]:
    """The breaker, or a closed one when it is absent or unreadable."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "closed", "streak": []}
    if not isinstance(value, dict) or value.get("state") not in ("open", "closed"):
        return {"state": "closed", "streak": []}
    if not isinstance(value.get("streak"), list):
        value["streak"] = []
    return value


def save(path: pathlib.Path, value: dict[str, Any]) -> None:
    tmp = path.with_name(f".breaker.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def boot_time() -> float | None:
    """The host's last boot, epoch seconds (`sysctl kern.boottime`)."""
    override = os.environ.get("TARTCI_VM_DHCP_BOOT_TIME")
    if override is not None:
        try:
            return float(override)
        except ValueError:
            return None
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                             text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"sec\s*=\s*(\d+)", out)
    return float(match.group(1)) if match else None


# `launchctl print` exits 113 ("Could not find service") for a job launchd
# has not loaded. bootpd's plist ships Disabled and only Internet Sharing
# (com.apple.NetworkSharing) loads it, so "not loaded" is its own failure: a
# kickstart of bootpd then fails, and the job must be loaded first.
LAUNCHCTL_NOT_FOUND = 113


def bootpd_readout() -> dict[str, Any]:
    """bootpd's launchd state, readable without root.

    {"loaded": True, "state", "runs", "last_exit"} for a loaded job,
    {"loaded": False, "state": "not_loaded"} when launchd does not have it,
    and {} when it could not be read.
    """
    launchctl = os.environ.get("TARTCI_VM_DHCP_LAUNCHCTL", "launchctl")
    try:
        proc = subprocess.run([launchctl, "print", "system/com.apple.bootpd"],
                              capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        text = f"{proc.stdout}\n{proc.stderr}"
        if proc.returncode == LAUNCHCTL_NOT_FOUND or "Could not find service" in text:
            return {"loaded": False, "state": "not_loaded"}
        return {}
    out: dict[str, Any] = {"loaded": True}
    for line in proc.stdout.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if not sep:
            continue
        if key == "state" and "state" not in out:
            out["state"] = value.strip()
        elif key == "runs":
            try:
                out["runs"] = int(value.strip())
            except ValueError:
                pass
        elif key == "last exit code":
            out["last_exit"] = value.strip()
    return out


def vm_network_readout() -> dict[str, Any]:
    """The host's vmnet shared-mode interfaces (bridge1NN), readable without
    root; {} when unreadable."""
    ifconfig = os.environ.get("TARTCI_VM_DHCP_IFCONFIG", "ifconfig")
    try:
        proc = subprocess.run([ifconfig, "-l"], capture_output=True, text=True, timeout=5,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    names = proc.stdout.split()
    return {"bridges": sorted(n for n in names if re.fullmatch(r"bridge1\d\d", n)),
            "vmenet": sum(1 for n in names if n.startswith("vmenet"))}


def bootpd_config_readout() -> dict[str, Any]:
    """/etc/bootpd.plist (world-readable): which interfaces get DHCP, and its
    mtime. {"present": False} when absent; {} when unreadable."""
    import plistlib
    path = pathlib.Path(os.environ.get("TARTCI_VM_DHCP_BOOTPD_PLIST", "/etc/bootpd.plist"))
    try:
        raw = path.read_bytes()
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return {"present": False}
    except OSError:
        return {}
    try:
        value = plistlib.loads(raw)
    except Exception:  # noqa: BLE001 - any malformed plist is unreadable
        return {}
    enabled = value.get("dhcp_enabled") if isinstance(value, dict) else None
    return {"present": True, "mtime": mtime,
            "dhcp_enabled": [str(x) for x in enabled] if isinstance(enabled, list) else []}


def sharing_pid() -> int | None:
    """InternetSharing's pid (com.apple.NetworkSharing), readable without root."""
    launchctl = os.environ.get("TARTCI_VM_DHCP_LAUNCHCTL", "launchctl")
    try:
        proc = subprocess.run([launchctl, "print", "system/com.apple.NetworkSharing"],
                              capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
        key, sep, value = line.strip().partition(" = ")
        if sep and key == "pid":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def diagnose(net: dict[str, Any], bootpd: dict[str, Any],
             config: dict[str, Any]) -> str:
    """Which layer failed, read while a VM that got no address is still up."""
    if not net:
        return "unknown"
    bridges = net.get("bridges") or []
    if not bridges:
        return "vm_network_missing"
    if bootpd.get("loaded") is False:
        return "bootpd_not_loaded"
    if config.get("present") is False or (
            config.get("present") and not set(bridges) & set(config.get("dhcp_enabled") or [])):
        return "dhcp_config_disabled"
    return "dhcp_silent"


def chain() -> dict[str, Any]:
    """What an operator's fix changes: compared between looks, a difference
    triggers a probe at once. Every part is readable without root."""
    bootpd = bootpd_readout()
    config = bootpd_config_readout()
    return {"bootpd_loaded": bootpd.get("loaded"), "bootpd_runs": bootpd.get("runs"),
            "config_mtime": config.get("mtime"), "sharing_pid": sharing_pid()}


def fmt(fields: dict[str, Any]) -> str:
    return " ".join(f"{k}={v}" for k, v in fields.items() if v is not None and v != "")


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def close(value: dict[str, Any], now: float, reason: str) -> list[list[str]]:
    opened = float(value.get("opened_at") or now)
    possible = max(float(value.get("last_probe_at") or opened),
                   float(value.get("bootpd_moved_at") or 0))
    event = ["vm_dhcp_recovered", fmt({
        "reason": reason, "open_s": int(now - opened),
        "vms_spent": int(value.get("vms_spent") or 0),
        "probes": int(value.get("probes") or 0),
        "latency_s": int(now - possible) if reason != "host_reboot" else None,
    })]
    value.clear()
    value.update({"state": "closed", "streak": [], "last_ip_at": now})
    return [event]


def check(args: argparse.Namespace, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    probe_secs = setting("TARTCI_VM_DHCP_PROBE_SECS", PROBE_SECS)
    with locked(breaker_dir()) as path:
        value = load(path)
        events: list[list[str]] = []
        if value["state"] != "open":
            return {"action": "clone", "events": events}
        booted = boot_time()
        if booted is not None and float(value.get("opened_at") or now) < booted:
            events += close(value, now, "host_reboot")
            save(path, value)
            return {"action": "clone", "events": events}
        last = value.get("last_probe_at")
        if last is None:
            last = value.get("opened_at")
        due = now - float(now if last is None else last) >= probe_secs
        current = chain()
        seen = value.get("chain")
        moved = isinstance(seen, dict) and current != seen
        value["chain"] = current
        if moved:
            value["bootpd_moved_at"] = now
        requested = value.get("probe_requested_at")
        asked = requested is not None and float(requested) > float(last or 0)
        if not (due or moved or asked):
            if not isinstance(seen, dict) or moved:
                save(path, value)
            return {"action": "backoff", "events": events}
        value["last_probe_at"] = now
        value["probe_lane"] = args.lane
        value["probes"] = int(value.get("probes") or 0) + 1
        save(path, value)
        events.append(["vm_dhcp_probe_start", fmt({
            "lane": args.lane,
            "trigger": "operator" if asked else "chain_changed" if moved else "cadence",
            "probe": value["probes"]})])
        return {"action": "probe", "events": events}


def record(args: argparse.Namespace, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    k = setting("TARTCI_VM_DHCP_K", K)
    window = setting("TARTCI_VM_DHCP_WINDOW_SECS", WINDOW_S)
    with locked(breaker_dir()) as path:
        value = load(path)
        events: list[list[str]] = []
        probe = value.get("state") == "open" and value.get("probe_lane") == args.lane
        if probe:
            events.append(["vm_dhcp_probe", fmt({"lane": args.lane, "vm": args.vm,
                                                 "result": args.outcome})])
            value["probe_lane"] = None
        if args.outcome == "ip":
            if value.get("state") == "open":
                events += close(value, now, "probe" if probe else "boot_ok")
            else:
                value["streak"] = []
                value["last_ip_at"] = now
            save(path, value)
            return {"action": "recorded", "events": events}
        # no_ip: read which layer failed while this VM is still up.
        readout = bootpd_readout()
        cause = diagnose(vm_network_readout(), readout, bootpd_config_readout())
        value["cause"], value["cause_at"] = cause, now
        if probe:
            events[-1][1] += f" cause={cause}"
        if value.get("state") == "open":
            value["vms_spent"] = int(value.get("vms_spent") or 0) + 1
            save(path, value)
            return {"action": "recorded", "events": events}
        streak = [row for row in value.get("streak") or []
                  if isinstance(row, dict) and now - float(row.get("ts") or 0) <= window]
        streak.append({"ts": now, "lane": args.lane, "vm": args.vm})
        value["streak"] = streak[-MAX_STREAK:]
        if len(streak) >= k:
            value.update({"state": "open", "opened_at": now, "vms_spent": len(streak),
                          "probes": 0, "last_probe_at": now, "chain": chain(),
                          "bootpd_runs": readout.get("runs"), "bootpd_moved_at": None,
                          "probe_requested_at": None})
            events.append(["vm_dhcp_unanswered", fmt({
                "streak": len(streak), "window_s": int(now - float(streak[0]["ts"])),
                "lanes": ",".join(sorted({str(r.get("lane")) for r in streak})),
                "last_no_ip": ",".join(iso(float(r["ts"])) for r in streak),
                "bootpd_state": readout.get("state", "unreadable"),
                "bootpd_runs": readout.get("runs"),
                "bootpd_last_exit": readout.get("last_exit"),
                "cause": cause,
            })])
        save(path, value)
        return {"action": "recorded", "events": events}


def probe_now(now: float | None = None) -> dict[str, Any]:
    """After a human fix: the next `check` probes at once instead of waiting
    out PROBE_SECS. A no-op on a closed breaker."""
    now = time.time() if now is None else now
    with locked(breaker_dir()) as path:
        value = load(path)
        if value.get("state") != "open":
            return {"action": "closed", "events": []}
        value["probe_requested_at"] = now
        save(path, value)
        return {"action": "requested", "events": [["vm_dhcp_probe_requested", fmt({
            "open_s": int(now - float(value.get("opened_at") or now)),
            "cause": value.get("cause")})]]}


def status(directory: pathlib.Path | None = None) -> dict[str, Any]:
    """For `tartci doctor fleet`: never a write, never a lock.

    An open breaker also carries bootpd's launchd state read now, so the
    doctor can tell a job that is not loaded from one that is loaded but
    silent: the two need different remedies.
    """
    path = (directory or breaker_dir()) / "breaker.json"
    if not path.exists():
        return {"state": "closed", "source": "absent"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"state": "unreadable", "error": str(exc)}
    if not isinstance(value, dict) or value.get("state") not in ("open", "closed"):
        return {"state": "unreadable", "error": "unexpected breaker shape"}
    if value.get("state") == "open":
        value["bootpd"] = bootpd_readout()
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("--lane", required=True)
    r = sub.add_parser("record")
    r.add_argument("--outcome", choices=["ip", "no_ip"], required=True)
    r.add_argument("--lane", required=True)
    r.add_argument("--vm", default="")
    sub.add_parser("probe-now")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(status(), sort_keys=True))
            return 0
        if args.command == "probe-now":
            result = probe_now()
        else:
            result = check(args) if args.command == "check" else record(args)
    except Exception as exc:  # noqa: BLE001 - the caller fails open
        print(json.dumps({"action": "error", "error": str(exc), "events": []}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
