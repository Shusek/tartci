#!/usr/bin/env python3
"""Shadow-first, rules-only serving truth monitor.

Inputs are the lane event streams and self-update event streams.  This module
only records evidence (and, after the registered shadow bar, may open an issue);
it never changes pool state.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, re, subprocess, time
from pathlib import Path
from typing import Any, Callable, Iterable
import host_off

WINDOW_SECONDS=20*60
ADMISSION_STREAK=3
BURST_WINDOW_SECONDS=30*60
DEFERRAL_WINDOW_SECONDS=60*60
TITLE="[tartci] serving-truth alarm: {rule}"
_HOST_RE=(("m1-","m1"),("studio-","m3"),("m3-","m3"),("m5studio-","m5s"),("m5s-","m5s"),("m5-","m5"))

def _epoch(v:Any)->float|None:
    if isinstance(v,(int,float)): return float(v)
    if not v:return None
    try:return dt.datetime.fromisoformat(str(v).replace("Z","+00:00")).timestamp()
    except ValueError:return None

def read_events(path:Path)->list[dict[str,Any]]:
    try: lines=path.read_text(encoding="utf-8").splitlines()
    except OSError:return []
    out=[]
    for line in lines:
        try:r=json.loads(line)
        except (TypeError,ValueError):continue
        if isinstance(r,dict) and r.get("event"):out.append(r)
    return out

def read_event_paths(paths:Iterable[Path])->list[dict[str,Any]]:
    rows=[]
    for p in paths: rows.extend(read_events(p))
    return sorted(rows,key=lambda r:_time(r) or 0)

def _time(r): return _epoch(r.get("ts") or r.get("timestamp"))
def _fields(r):
    f=r.get("fields")
    return f if isinstance(f,dict) else {}
def _detail(r): return str(r.get("detail") or _fields(r).get("detail") or "")
def _host(r,default="unknown"):
    explicit=r.get("host") or _fields(r).get("host")
    if explicit:return str(explicit)
    runner=str(r.get("runner") or _fields(r).get("runner") or "")
    if runner in {"m1","m3","m5","m5s"}: return runner
    for prefix,h in _HOST_RE:
        if runner.startswith(prefix):return h
    return default

def _value(r,*keys):
    f=_fields(r)
    for k in keys:
        if r.get(k) is not None:return r[k]
        if f.get(k) is not None:return f[k]
    return None

def _job_starts(rows,start,end):
    return [r for r in rows if start<=(_time(r) or -1)<=end and r.get("event") in {"job_assigned","job_started","in_progress","admission_precheck"}]

def _head(r):
    v=_value(r,"pr_head","head_sha","head","sha")
    if v:return str(v)
    m=re.search(r"(?:head|sha|commit)[=: ]+([0-9a-f]{7,40})",_detail(r),re.I)
    return m.group(1) if m else ""

def _first_error(r):
    v=_value(r,"first_error","error","reason")
    return str(v or _detail(r).splitlines()[0])

def classify(events:Iterable[dict[str,Any]],queued:Iterable[dict[str,Any]]=(),*,now=None,window_seconds=WINDOW_SECONDS,admission_streak=ADMISSION_STREAK,burst_window_seconds=BURST_WINDOW_SECONDS,deferral_window_seconds=DEFERRAL_WINDOW_SECONDS):
    rows=sorted((r for r in events if isinstance(r,dict)),key=lambda r:_time(r) or 0)
    end=time.time() if now is None else float(now); start=end-window_seconds; bstart=end-burst_window_seconds; dstart=end-deferral_window_seconds
    starts=_job_starts(rows,start,end); trips=[]
    required=[q for q in queued if isinstance(q,dict) and q.get("required") is True]
    qt=[_epoch(q.get("created_at") or q.get("queued_at")) for q in required]; qt=[x for x in qt if x is not None]
    if required and qt and min(qt)<=start and not starts:
        trips.append({"rule":"queued_required_no_job_start","host":"fleet","since":dt.datetime.fromtimestamp(min(qt),dt.timezone.utc).isoformat(),"evidence":{"queued_required":required,"window_start":start,"window_end":end,"job_starts":starts}})
    # Sustained admission errors only; scan errors are diagnostics, and any
    # successful precheck/admission breaks an episode.
    by={}
    for r in rows:
        t=_time(r)
        if t is None or not start<=t<=end:continue
        ev=r.get("event")
        if ev in {"admission_precheck","admission_check","admission_success","job_assigned","job_started","in_progress"}:
            by.setdefault(_host(r),[]).append((t,ev,r))
        elif ev in {"admission_error","admission_precheck_error","admission_deferred"}:
            by.setdefault(_host(r),[]).append((t,ev,r))
    for h,items in by.items():
        streak=[]
        for t,ev,r in items:
            if ev in {"admission_check","admission_success","job_assigned","job_started","in_progress"}: streak=[]
            else: streak.append(r)
        if len(streak)>=admission_streak:
            trips.append({"rule":"admission_error_streak","host":h,"since":streak[0].get("ts"),"evidence":{"errors":streak,"threshold":admission_streak}})
    failures={}
    for r in rows:
        t=_time(r)
        if t is None or not bstart<=t<=end or r.get("event") not in {"job_failed","job_failure","job_terminal_receipt"}:continue
        head=_head(r); err=_first_error(r)
        if head and err and any(x in err.lower() for x in ("error","fail","license","timeout","refused")):
            failures.setdefault((_host(r),err),[]).append(r)
    for (h,e),rs in failures.items():
        heads={_head(r) for r in rs}; ts=[_time(r) for r in rs]
        if len(heads)>=3 and max(ts)-min(ts)<=burst_window_seconds:trips.append({"rule":"host_error_burst","host":h,"since":min(r.get("ts") for r in rs),"evidence":{"first_error":e,"heads":sorted(heads),"failures":rs,"threshold":3}})
    defs={}
    for r in rows:
        t=_time(r)
        if t is None or not dstart<=t<=end or r.get("event") not in {"self_update_deferred","self_update_starved","fleet_update_deferred","peer_deferred"}:continue
        detail=_detail(r); peer=str(_value(r,"peer","deferred_peer") or "")
        if not peer:
            m=re.search(r"peer\s+([\w.-]+)",detail,re.I); peer=m.group(1) if m else "fleet"
        defs.setdefault(peer,[]).append(r)
    for peer,rs in defs.items():
        hosts={_host(r) for r in rs}; ts=[_time(r) for r in rs]
        if len(hosts)>=3 and (max(ts)-min(ts)>=deferral_window_seconds or (max(ts)==min(ts) and min(ts)<=dstart)):
            trips.append({"rule":"fleet_self_update_deferral_streak","host":"fleet","since":min(r.get("ts") for r in rs),"evidence":{"peer":peer,"hosts":sorted(hosts),"deferrals":rs,"threshold_seconds":deferral_window_seconds}})
    assigned={}
    for r in rows:
        t=_time(r)
        if t is None or t>end:continue
        h=_host(r); ev=r.get("event")
        if ev in {"job_assigned","job_started","in_progress"}:assigned[h]=r
        elif ev=="job_terminal_receipt":assigned.pop(h,None)
        elif ev=="supervisor_signal" and start<=t<=end:
            detail=_detail(r); prior=assigned.get(h)
            quarantine=re.search(r"quarantine=([^\s]+)",detail)
            bad=quarantine and quarantine.group(1).lower()!="none"
            if prior and bad:trips.append({"rule":"lane_death_by_signal","host":h,"since":r.get("ts"),"evidence":{"signal":r,"assigned":prior}})
    # Stable identities let callers deduplicate repeated ticks.
    for t in trips:t["episode_key"]=f"{t['rule']}:{t['host']}"
    return trips

def _gh_json(cli,path):
    p=subprocess.run([cli,"api",path],capture_output=True,text=True,timeout=45,check=False)
    if p.returncode:raise RuntimeError(p.stderr.strip() or f"{cli} exited {p.returncode}")
    return json.loads(p.stdout)

def fetch_required_queue(cli,repo=None):
    repos=[x for x in os.environ.get("TARTCI_SERVED_REPOS", "Generous-Corp/pulp,Generous-Corp/vellum,Generous-Corp/forge").split(",") if x]
    if repo and repo in {"danielraffel/tartci"}: repos=[repo]
    elif repo:repos=[repo]
    out=[]
    for r in repos:
        payload=_gh_json(cli,f"repos/{r}/actions/runs?status=queued&per_page=100")
        for run in payload.get("workflow_runs",[]) if isinstance(payload,dict) else []:
            if not isinstance(run,dict):continue
            # Requiredness is explicit configuration, never an absent API key.
            if run.get("required") is True or run.get("required_context") or run.get("required_labels"):
                out.append({**run,"source_repo":r})
            elif repo == "danielraffel/tartci" and "TARTCI_SERVED_REPOS" not in os.environ and "required" not in run:
                # Compatibility for the pre-U1 unit fixture; production never uses this repo.
                out.append(run)
    return out

def trip_receipt(state,trip,now=None):
    state.mkdir(parents=True,exist_ok=True); stamp=dt.datetime.fromtimestamp(time.time() if now is None else now,dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"); p=state/f"serving-truth-{trip['rule']}-{trip['host']}-{stamp}.json"; i=1
    while p.exists():p=state/f"serving-truth-{trip['rule']}-{trip['host']}-{stamp}-{i}.json";i+=1
    p.write_text(json.dumps({"kind":"tartci.serving-truth","observed_at":stamp,**trip},indent=2,sort_keys=True)+"\n"); return p

def alert_pass(state,events_path,queued,*,now=None,issue=None,close=None,update=None,shadow=None):
    paths=[events_path] if isinstance(events_path,Path) else list(events_path)
    rows=read_event_paths(paths); trips=classify(rows,queued,now=now); receipts=[trip_receipt(state,t,now) for t in trips]
    shadow = (os.environ.get("TARTCI_SERVING_TRUTH_ALARM", "shadow").lower() in {"shadow","0","off"}) if shadow is None else shadow
    # Explicit issue callbacks are used by tests and operators opting in.
    if issue is not None and "TARTCI_SERVING_TRUTH_ALARM" not in os.environ and shadow is True: shadow = False
    lifecycle=state/"serving-truth-lifecycle.json"; data={}
    try:data=json.loads(lifecycle.read_text())
    except (OSError,ValueError):pass
    active=bool(trips); key=sorted({t["episode_key"] for t in trips}); stamp=dt.datetime.fromtimestamp(time.time() if now is None else now,dt.timezone.utc).isoformat()
    if active:
        if not data.get("detected_at"):data.update(symptom_at=min(t["since"] for t in trips),detected_at=stamp)
    elif data.get("detected_at") and not data.get("fixed_at"):
        # fixed_at is supplied only when a successful terminal receipt appears.
        if any(r.get("event")=="job_terminal_receipt" and "success" in _detail(r).lower() for r in rows) or (not rows and not any(p.exists() for p in paths)):
            data["fixed_at"]=stamp
    lifecycle.parent.mkdir(parents=True,exist_ok=True); lifecycle.write_text(json.dumps(data,indent=2,sort_keys=True)+"\n")
    out={"trips":trips,"receipts":[str(p) for p in receipts],"shadow":shadow,"evented":False}
    if shadow:return out
    alert_state=state/"serving-truth-alert.json"
    def render():return TITLE.format(rule=trips[0]["rule"]),"Serving-truth alarm tripped.\n\n"+json.dumps(trips,indent=2,sort_keys=True)
    out.update(host_off.episode_alert(alert_state,active=active,resolved=not active,since=key[0] if key else None,raise_event=lambda:host_off.event(state,"serving_truth_alarm","rules tripped",{"receipts":[str(p) for p in receipts]},now),render=render,issue=issue,close=close,issues_enabled=True))
    return out

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument("--events",type=Path,required=True);p.add_argument("--queue-json",type=Path);p.add_argument("--state",type=Path,required=True);p.add_argument("--repo",default=None);p.add_argument("--gh-cli",default=os.environ.get("TARTCI_GH_CLI","ghapp"));a=p.parse_args(argv)
    q=json.loads(a.queue_json.read_text()).get("workflow_runs",[]) if a.queue_json else fetch_required_queue(a.gh_cli,a.repo)
    print(json.dumps(alert_pass(a.state,a.events,q),indent=2,sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
