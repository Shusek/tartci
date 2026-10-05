#!/usr/bin/env python3
"""Do a host's gate lanes fit its gate reserve? A ratchet, not a gate.

A gate lease is admitted against the host's whole lease capacity, so a gate
slot can always start on an idle host. But agent builds may hold every core
outside the gate reserve, so on a busy host the lane's slots run together only
if they fit inside that reserve. m3, 2026-10-04: two Pulp gate slots of 12
cores against a 14-core reserve; the second slot logged `lease_denied
axis=cores reason=capacity_exceeded requested_cores=12` 8 times and macos jobs
queued (#373 resized them to 7).

The reserve differs per host: it is computed by host_profile.py from the
host's cores, memory, role and governor settings, so the fit is computed from
this host's live host-profile, per gate lane and per axis:

    cores   supervisors x vm_cores (default: the host's vm_pool_cores)
            against reserved_gate_cores
    memory  supervisors x the VM memory derived from those cores (the same
            rule as vm-lease.lib.sh) against reserved_gate_mem_mb

A gate lane is one without an explicit priority (the Pulp gate's event
classes lease at gate priority) or with `priority = "gate"`.

Two hosts overcommit today (m1: 2 x 3 against 3; m5: 2 x 6 against 8).
Refusing them would turn a capacity finding into two hosts that can never
update, so this is a RATCHET: every overcommitted (lane, axis) is reported on
every evaluation, and a target profile is refused only when its overcommit on
some (lane, axis) is strictly greater than the installed profile's on the
same pair, both measured against the same live reserve. Equal is allowed;
smaller is the direction wanted. Resizing is a profile decision with the
host's owner, never automatic, and must not take agent cores.

Python 3.9-safe.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

PER_JOB_MEM_MB = 1536
VM_MIN_MEM_MB = 8192
VM_MAX_MEM_MB = 16384
AXES = ("cores", "memory")


def vm_mem_mb(cores: int, per_job: int = PER_JOB_MEM_MB) -> int:
    """tartci_vm_lease_derived_mem_mb: (cores - 1) x per-job x 4/3, clamped."""
    jobs = max(1, cores - 1)
    return max(VM_MIN_MEM_MB, min(VM_MAX_MEM_MB, jobs * per_job * 4 // 3))


def gate_lanes(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [lane for lane in profile.get("lane", []) or []
            if isinstance(lane, dict) and lane.get("priority") in (None, "gate")]


def fit(profile: Dict[str, Any], host: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One row per gate lane and axis: demand, reserve and overcommit."""
    rows = []
    per_job = int(host.get("per_compile_job_mem_mb") or PER_JOB_MEM_MB)
    for lane in gate_lanes(profile):
        supervisors = int(lane.get("supervisors", 1))
        cores = int(lane.get("vm_cores") or host["vm_pool_cores"])
        demand = {"cores": supervisors * cores,
                  "memory": supervisors * vm_mem_mb(cores, per_job)}
        reserve = {"cores": int(host["reserved_gate_cores"]),
                   "memory": int(host.get("reserved_gate_mem_mb") or 0)}
        for axis in AXES:
            if reserve[axis] <= 0:
                # No gate reserve on this axis (memory unread, or a host whose
                # role reserves nothing for gates): there is no reserve for the
                # slots to fit inside, so there is nothing to measure.
                continue
            rows.append({"lane": str(lane.get("id")), "axis": axis,
                         "demand": demand[axis], "reserve": reserve[axis],
                         "over": max(0, demand[axis] - reserve[axis])})
    return rows


def finding_lines(rows: List[Dict[str, Any]]) -> List[str]:
    return [f"gate_reserve_overcommitted lane={r['lane']} axis={r['axis']} "
            f"demand={r['demand']} reserve={r['reserve']}" for r in rows if r["over"] > 0]


def ratchet(installed: Optional[Dict[str, Any]], target: Dict[str, Any],
            host: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(target rows, refusals). A refusal is a (lane, axis) the target makes worse.

    With no installed profile (a first install), nothing is refused: there is
    nothing to ratchet against, and the findings still report.
    """
    rows = fit(target, host)
    if installed is None:
        return rows, []
    before = {(r["lane"], r["axis"]): r["over"] for r in fit(installed, host)}
    refusals = [f"gate_reserve_worse lane={r['lane']} axis={r['axis']} "
                f"installed_over={before.get((r['lane'], r['axis']), 0)} "
                f"target_over={r['over']} reserve={r['reserve']}"
                for r in rows if r["over"] > before.get((r["lane"], r["axis"]), 0)]
    return rows, refusals
