#!/usr/bin/env python3
"""Whether this host's keychain setup can put a password dialog on screen.

A dialog appears when a process in the GUI session touches a non-login
keychain that is locked in that session: securityd starts SecurityAgent and
asks for that keychain's password, which only keychain.env holds. On
2026-10-02 the legacy `pulp-signing.keychain-db` sat on m3's and m5studio's
user search list beside its `-unattended` replacement. A bare
`codesign --sign` walks that list, so every unpinned signer was one lock away
from a dialog asking Daniel for a password he does not have.

status() reports, without ever prompting:
  - every keychain on the user search list other than login and the dedicated
    signing keychain (a legacy or leftover keychain is what a search walks
    into);
  - whether the dedicated keychain unlocks with keychain.env's password
    (`unlock-keychain -p` never prompts; a refusal means the recorded
    password has drifted);
  - after that unlock, so reading settings cannot prompt, whether the keychain
    re-locks on its own (an inactivity timeout or lock-on-sleep).

States: ok | risk | not_applicable (no keychain.env) | unknown.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_self_update as su  # noqa: E402

Runner = Callable[[list[str]], tuple[int, str]]


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def search_list(text: str) -> list[str]:
    return [line.strip().strip('"') for line in text.splitlines() if line.strip()]


def status(home: Path | None = None, run: Runner = _run) -> dict[str, Any]:
    home = home or Path.home()
    secrets = su.signing_secrets(home)
    dedicated = su.signing_keychain(home)
    password = secrets.get("PULP_SIGN_KEYCHAIN_PW")
    if not dedicated or not password:
        return {"state": "not_applicable", "risks": [],
                "detail": "no dedicated signing keychain in keychain.env"}
    rc, out = run(["security", "list-keychains", "-d", "user"])
    if rc != 0:
        return {"state": "unknown", "risks": [], "detail": f"search list unreadable: {out[:200]}"}
    risks = []
    for path in search_list(out):
        if Path(path).name == "login.keychain-db" or path == dedicated:
            continue
        risks.append(f"{path} is on the user search list beside the dedicated "
                     f"{Path(dedicated).name}; an unpinned codesign that walks into it while "
                     "it is locked raises a password dialog")
    rc, out = run(["security", "unlock-keychain", "-p", password, dedicated])
    if rc != 0:
        risks.append(f"{dedicated} does not unlock with keychain.env's password "
                     f"({out.strip()[:120]}); run `pulp ship doctor`")
    else:
        rc, out = run(["security", "show-keychain-info", dedicated])
        if rc == 0 and (re.search(r"timeout=\d+", out) or "lock-on-sleep" in out):
            risks.append(f"{dedicated} re-locks on its own ({out.strip()[:120]}); "
                         "`pulp ship doctor` sets it to no-timeout")
    return {"state": "risk" if risks else "ok", "risks": risks, "dedicated": dedicated}


def describe(value: dict[str, Any]) -> str:
    state = value.get("state")
    if state == "ok":
        return f"signing prompts: ok (only {Path(value['dedicated']).name} and login searched)"
    if state == "risk":
        return "signing prompts: RISK: " + " | ".join(value["risks"])
    if state == "not_applicable":
        return ""
    return f"signing prompts: UNKNOWN ({value.get('detail')})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="signing_prompt_guard")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    value = status()
    if args.json:
        print(json.dumps(value, sort_keys=True))
    else:
        line = describe(value)
        if line:
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
