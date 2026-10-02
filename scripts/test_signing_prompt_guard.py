#!/usr/bin/env python3
"""The keychain setup is checked for anything that can raise a password dialog.

On 2026-10-02 the legacy pulp-signing keychain sat on m3's and m5studio's
search list beside its -unattended replacement, and the self-update probe's
bare `codesign --sign` put dialogs on m5studio's screen at each attempt.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fleet_doctor as fd  # noqa: E402
import fleet_self_update as su  # noqa: E402
import keychain_unlock  # noqa: E402
import signing_prompt_guard as guard  # noqa: E402


class Host(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.kc = self.home / "Library" / "Keychains"
        self.kc.mkdir(parents=True)
        self.legacy = self.kc / "pulp-signing.keychain-db"
        self.sibling = self.kc / "pulp-signing-unattended.keychain-db"
        self.login = self.kc / "login.keychain-db"
        for path in (self.legacy, self.sibling, self.login):
            path.write_text("")
        secrets = self.home / ".config" / "pulp" / "secrets"
        secrets.mkdir(parents=True)
        (secrets / "keychain.env").write_text(
            f'PULP_SIGN_KEYCHAIN="{self.legacy}"\nPULP_SIGN_KEYCHAIN_PW=pw\n')
        self.listed = [self.sibling, self.login]
        self.unlock_rc = 0
        self.info = f'Keychain "{self.sibling}" no-timeout'
        self.calls: list[list[str]] = []
        env = mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.home / ".tartci")})
        env.start()
        self.addCleanup(env.stop)
        self.agent(state="ok", age=60)

    def agent(self, state: str | None, age: float = 60) -> None:
        plist = self.home / "Library" / "LaunchAgents" / f"{keychain_unlock.LABEL}.plist"
        if state is None:
            plist.unlink(missing_ok=True)
            return
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text("<plist/>")
        path = keychain_unlock.state_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"state": state, "at": time.time() - age,
                                    "detail": "unlock failed (exit 51)" if state == "failed"
                                    else "unlocked, no auto-lock"}))

    def run_(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[1] == "list-keychains":
            return 0, "".join(f'    "{p}"\n' for p in self.listed)
        if argv[1] == "unlock-keychain":
            return self.unlock_rc, "" if self.unlock_rc == 0 else "The specified keychain could not be unlocked"
        if argv[1] == "show-keychain-info":
            return 0, self.info
        raise AssertionError(argv)

    def status(self) -> dict:
        return guard.status(self.home, self.run_)


class GuardTests(Host):
    def test_only_the_dedicated_keychain_and_login_is_ok(self) -> None:
        value = self.status()
        self.assertEqual(value["state"], "ok", value)
        self.assertIn("pulp-signing-unattended.keychain-db", guard.describe(value))
        self.assertEqual(fd.check_signing_prompts(value, self.home).code, "signing_prompts_ok")

    def test_a_legacy_keychain_on_the_search_list_is_a_risk(self) -> None:
        # m3 on 2026-10-02: legacy first, then the sibling, then login.
        self.listed = [self.legacy, self.sibling, self.login]
        value = self.status()
        self.assertEqual(value["state"], "risk")
        self.assertIn(str(self.legacy), value["risks"][0])
        finding = fd.check_signing_prompts(value, self.home)
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "signing_prompts_risk"))

    def test_a_drifted_password_is_a_risk_and_settings_are_not_read_locked(self) -> None:
        self.unlock_rc = 51
        value = self.status()
        self.assertEqual(value["state"], "risk")
        self.assertIn("does not unlock", value["risks"][0])
        # Reading settings of a locked keychain is itself what prompts.
        self.assertFalse(any(c[1] == "show-keychain-info" for c in self.calls))

    def test_a_keychain_that_relocks_on_its_own_is_a_risk(self) -> None:
        self.info = f'Keychain "{self.sibling}" lock-on-sleep timeout=300s'
        value = self.status()
        self.assertEqual(value["state"], "risk")
        self.assertIn("re-locks on its own", value["risks"][0])

    def test_a_host_without_keychain_env_is_not_applicable(self) -> None:
        (self.home / ".config" / "pulp" / "secrets" / "keychain.env").unlink()
        self.assertEqual(self.status()["state"], "not_applicable")
        self.assertEqual(self.calls, [])


class UnlockAgentGuardTests(Host):
    """The keychain is locked again at every login; the agent is what unlocks it."""

    def test_a_missing_agent_is_a_risk(self) -> None:
        self.agent(None)
        value = self.status()
        self.assertEqual(value["state"], "risk")
        self.assertIn("keychain-unlock agent is not installed", value["risks"][0])

    def test_a_failed_last_run_is_a_risk(self) -> None:
        self.agent("failed")
        self.assertIn("last run FAILED", self.status()["risks"][0])

    def test_an_agent_that_stopped_running_is_a_risk(self) -> None:
        self.agent("ok", age=guard.UNLOCK_STALE_SECS + 60)
        self.assertIn("min ago", self.status()["risks"][0])


class PinnedKeychainTests(Host):
    def test_the_sibling_is_the_dedicated_keychain_when_it_exists(self) -> None:
        self.assertEqual(su.signing_keychain(self.home), str(self.sibling))
        self.assertEqual(su.keychain_args(self.home), ["--keychain", str(self.sibling)])
        self.sibling.unlink()
        self.assertEqual(su.signing_keychain(self.home), str(self.legacy))

    def test_the_probe_names_the_keychain_instead_of_walking_the_search_list(self) -> None:
        seen = []

        class Sys:
            def run(self, argv, **kw):
                seen.append(argv)
                return su.Result(0, "", "")
        su.signing_probe(Sys(), "ABC", self.home, su.keychain_args(self.home))
        self.assertIn(["--keychain", str(self.sibling)],
                      [seen[0][i:i + 2] for i in range(len(seen[0]) - 1)])

    def test_the_launcher_build_signs_with_the_named_keychain(self) -> None:
        script = (HERE / "build_macos_launcher.sh").read_text()
        self.assertIn('--keychain) keychain="${2:-}"', script)
        self.assertRegex(script, r'codesign --force --timestamp --options runtime '
                                 r'\$\{keychain_args\[@\]\+"\$\{keychain_args\[@\]\}"\} --sign')
        source = (HERE / "fleet_self_update.py").read_text()
        body = source[source.index("def build_launcher("):]
        self.assertIn('"--profile", str(profile), *pinned]', body[:body.index("\ndef ")])


if __name__ == "__main__":
    unittest.main()
