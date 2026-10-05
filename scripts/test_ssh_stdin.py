#!/usr/bin/env python3
"""No ssh invocation in the repo may leave its stdin implicit.

#371: `gate_supply.py decide` ran `[ssh, "-o", "BatchMode=yes", ...]` without
`-n` from inside the supervisor's `while read ... done <<< "$classes"` loop;
ssh forwarded the rest of the class list to the peer, and the loop ended after
the first class with young demand. shellcheck's SC2095 sees only an ssh
written directly in the loop, so `scripts/ssh_stdin_check.py` checks every
invocation instead. These tests run it on the repo (must be clean) and on
fixtures that pin what it flags and what it leaves alone.
"""

from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ssh_stdin_check as check  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def shell(text: str) -> list:
    return list(check.shell_findings(Path("f.sh"), textwrap.dedent(text)))


def python(text: str) -> list:
    return list(check.python_findings(Path("f.py"), textwrap.dedent(text)))


class RepoTests(unittest.TestCase):
    def test_every_ssh_invocation_in_the_repo_states_its_stdin(self) -> None:
        found = check.findings(ROOT)
        self.assertEqual(found, [], "\n".join(found))


class PythonTests(unittest.TestCase):
    def test_the_371_peer_read_is_flagged_and_its_fix_is_not(self) -> None:
        before = '''
            command = [ssh, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={t}",
                       target, "tartci pool supply --json"]
        '''
        self.assertEqual(len(python(before)), 1)
        self.assertEqual(python(before.replace('[ssh, "-o"', '[ssh, "-n", "-o"')), [])

    def test_a_literal_ssh_argv_needs_n_and_a_name_list_is_not_an_argv(self) -> None:
        self.assertEqual(len(python('run(["ssh", "-o", "BatchMode=yes", host, "true"])')), 1)
        self.assertEqual(python('run(["ssh", "-n", "-o", "BatchMode=yes", host, "true"])'), [])
        self.assertEqual(python('if head in ("ssh", "autossh"): pass'), [])

    def test_an_exempted_argv_is_skipped(self) -> None:
        self.assertEqual(python('a = ["ssh", "-T", h]  # ssh-stdin: pipes a script in'), [])


class ShellTests(unittest.TestCase):
    def test_ssh_inside_a_function_is_flagged(self) -> None:
        # The function is where SC2095 goes blind: the loop is in the caller.
        found = shell('''
            peer_status(){ ssh -o BatchMode=yes "$1" "tartci pool status --json"; }
        ''')
        self.assertEqual(len(found), 1)
        self.assertIn("f.sh:2:", found[0])

    def test_command_substitution_and_array_definitions_are_invocations(self) -> None:
        self.assertEqual(len(shell('out="$(ssh -o BatchMode=yes "$h" uname)"\n')), 1)
        self.assertEqual(len(shell('SSH=(ssh -o ConnectTimeout=8 "$USER@127.0.0.1")\n')), 1)
        self.assertEqual(len(shell('if ! ssh "${SSH_OPTS[@]}" "$h" true; then x; fi\n')), 1)

    def test_a_stated_stdin_passes(self) -> None:
        for ok in ('ssh -n "$h" uname\n',
                   'ssh "$h" true </dev/null\n',
                   'printf %s "$jit" | ssh "$h" "cat > jit.cfg"\n',
                   "ssh \"$h\" bash -s <<'EOF'\n",
                   'ssh -G "$h" | awk \'/^user /{print $2}\'\n'):
            self.assertEqual(shell(ok), [], ok)

    def test_a_redirect_after_a_remote_command_with_separators_still_counts(self) -> None:
        # The `&&` is inside the quoted remote command; the redirect is ssh's.
        self.assertEqual(shell('''
            ssh "$h" \\
              "sudo mkdir -p /mnt/host && bash -s -- /mnt/host/ccache" \\
              <"$TARTCI_ROOT/prepare-ccache.sh"
        '''), [])

    def test_mentions_and_lookups_are_not_invocations(self) -> None:
        for text in ('note "booting (ssh 127.0.0.1:$port)"\n',
                     "echo 'run ssh host to check'\n",
                     'command -v ssh >/dev/null 2>&1 || die "ssh not installed"\n',
                     '# ssh host true\n'):
            self.assertEqual(shell(text), [], text)

    def test_an_exemption_on_the_line_or_the_line_above_is_honoured(self) -> None:
        self.assertEqual(shell('''
            # ssh-stdin: callers pipe the guest script in
            wsh(){ ssh "${SSH_OPTS[@]}" "$h" "$@"; }
        '''), [])
        self.assertEqual(shell('wsh(){ ssh "$h" "$@"; }  # ssh-stdin: callers pipe\n'), [])


if __name__ == "__main__":
    unittest.main()
