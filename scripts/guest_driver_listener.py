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
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--idle-timeout', type=int, required=True)
    parser.add_argument('--job-timeout', type=int, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or min(args.idle_timeout, args.job_timeout) <= 0:
        parser.error('command and positive deadlines are required')
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
