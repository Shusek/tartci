#!/usr/bin/env python3
"""Monitor a prepared guest transport; credentials travel only over stdin."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import queue
import threading
import time
from bounded_subprocess import run_bounded


def assignment_observation(repo, runner, policy):
    """Read the exact assigned job across the declared repository scope."""
    result = run_bounded([sys.executable, str(Path(__file__).with_name('current_job_scan.py')),
                             '--repo', repo, '--runner', runner, '--policy-file', str(policy),
                             '--scan-timeout', '120', '--parallelism', '1'],
                            timeout=130, operation='prepared_guest_assignment')
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError):
        return 'observation_error'
    return value.get('kind', 'observation_error')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--idle-timeout', type=int, required=True)
    parser.add_argument('--job-timeout', type=int, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--assignment-policy', type=Path)
    parser.add_argument('--repo')
    parser.add_argument('--runner')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or min(args.idle_timeout, args.job_timeout) <= 0:
        parser.error('command and positive deadlines are required')
    if args.assignment_policy and not (args.repo and args.runner):
        parser.error('assignment policy requires repository and runner identity')
    process = subprocess.Popen(command, stdin=sys.stdin.buffer, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    assigned = False
    interrupted = False
    started = time.monotonic()
    assigned_at = None
    receipt = {'pid': os.getpid(), 'transport_pid': process.pid, 'assigned': False, 'terminal': False}
    def save():
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.receipt.with_suffix('.tmp')
        temporary.write_text(json.dumps(receipt) + '\n')
        temporary.chmod(0o600)
        temporary.replace(args.receipt)
    def stop(signum, frame):
        nonlocal interrupted
        interrupted = True
        # A drain waits for an assigned job; an idle listener can be stopped.
        if not assigned:
            os.killpg(process.pid, signal.SIGTERM)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    save()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffered = b''
    code = 0
    observations = queue.Queue()
    observing = False
    next_observation = 0
    observation_errors = 0
    def observe():
        try:
            observations.put(assignment_observation(args.repo, args.runner, args.assignment_policy))
        except Exception:
            observations.put('observation_error')
    try:
        while selector.get_map() or process.poll() is None:
            for key, _ in selector.select(0.5):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()
                buffered = (buffered + chunk)[-131072:]
                if not assigned and b'Running job:' in buffered:
                    assigned = True; assigned_at = time.monotonic()
                    receipt['assigned'] = True; save()
            if assigned and args.assignment_policy:
                try:
                    verdict = observations.get_nowait()
                except queue.Empty:
                    verdict = None
                if verdict is not None:
                    observing = False
                    next_observation = time.monotonic() + 30
                    receipt['assignment_observation'] = verdict
                    observation_errors = observation_errors + 1 if verdict == 'observation_error' else 0
                    save()
                    if verdict in ('unexpected_assignment', 'policy_violation', 'assignment_changed') or observation_errors >= 3:
                        receipt['assignment_refused'] = True
                        code = 126
                        os.killpg(process.pid, signal.SIGTERM)
                        try: process.wait(timeout=10)
                        except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL)
                        break
                if not observing and time.monotonic() >= next_observation and process.poll() is None:
                    observing = True
                    threading.Thread(target=observe, daemon=True).start()
            elapsed = time.monotonic() - (assigned_at if assigned else started)
            if elapsed >= (args.job_timeout if assigned else args.idle_timeout):
                code = 124
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL)
                break
        result = process.wait(timeout=15)
        code = code or (128 + abs(result) if result < 0 else result)
        if interrupted and not assigned: code = code or 75
        return code
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL); process.wait()
        receipt.update(terminal=True, exit_code=code, drain_requested=interrupted)
        save(); selector.close()


if __name__ == '__main__':
    raise SystemExit(main())
