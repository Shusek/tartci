# Fleet operator scripts

One-off, idempotent host changes an operator runs by hand on a fleet Mac.
tartci never runs them. Each defaults to a read-only plan; the privileged
step needs `sudo` and refuses without it, printing the exact command.

| Script | Plan | Apply | What apply changes |
|---|---|---|---|
| `sshd-pubkey-only.sh` | `fleet/ops/sshd-pubkey-only.sh` | `sudo fleet/ops/sshd-pubkey-only.sh --apply` | Writes `/etc/ssh/sshd_config.d/100-fleet-pubkey-only.conf` (`PasswordAuthentication no`, `KbdInteractiveAuthentication no`) after proving the sudo user has key login to localhost. Undo: `sudo fleet/ops/sshd-pubkey-only.sh --rollback`. |
| `create-ci-runner-user.sh` | `fleet/ops/create-ci-runner-user.sh` | `sudo fleet/ops/create-ci-runner-user.sh --apply` | Creates the hidden, non-admin `ci` user with its own primary group (not `staff`), no password (login disabled), no SSH keys and no keychain secrets; sets the invoking user's home to 0700 if group/other can enter it (a stock home is 0750 owner:staff) and reports, without changing, `~/.ssh`, `~/.config/pulp/secrets` and `~/.config/shipyard/github-apps` modes; then prints the shell-quoted per-runner commands that move each persistent runner under `~/actions-ci/*/` onto it. It re-registers nothing itself. |

**sshd lockout guard scope.** It proves one key path only: the sudo user,
from this host, to localhost. Other accounts and keys on other machines are
not checked. If SSH access is lost after `--apply`, sign in at the local
console or over Screen Sharing (neither uses sshd) and run
`sudo fleet/ops/sshd-pubkey-only.sh --rollback`.

`--help` on either script explains why it exists. The dry-run tests are
`tests/test_fleet_ops_scripts.sh` (stub binaries and a temp dir; nothing on
the host changes).
