#!/usr/bin/env bash
# fleet/ops dry runs: sshd-pubkey-only.sh and create-ci-runner-user.sh plan,
# refuse and (against a temp dir and stub binaries only) apply. Nothing here
# touches /etc/ssh, launchd or the account database: FLEET_OPS_TEST=1 makes
# both scripts take every system command from PATH, which is all stubs.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSHD_OPS="$ROOT/fleet/ops/sshd-pubkey-only.sh"
CI_OPS="$ROOT/fleet/ops/create-ci-runner-user.sh"
tmp="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/fleet-ops-test.XXXXXX")" && pwd)"
trap 'rm -rf "$tmp"' EXIT
me="$(id -un)"
REAL_ID="$(command -v id)"

fail() { echo "FAIL: $*" >&2; exit 1; }
expect_rc() { # expect_rc <rc> <label> -- cmd...
  local want="$1" label="$2" rc=0; shift 3
  "$@" >"$tmp/out" 2>"$tmp/err" || rc=$?
  [ "$rc" = "$want" ] || { cat "$tmp/out" "$tmp/err" >&2; fail "$label: rc $rc, want $want"; }
}
has() { grep -qF -- "$2" "$tmp/$1" || { cat "$tmp/out" "$tmp/err" >&2; fail "missing in $1: $2"; }; }
lacks() { ! grep -qF -- "$2" "$tmp/$1" || fail "unexpected in $1: $2"; }

# ── stubs ────────────────────────────────────────────────────────────────────
bin="$tmp/bin"
mkdir -p "$bin"
cat >"$bin/ssh" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *PreferredAuthentications=none*)
    echo "nobody@127.0.0.1: Permission denied (${STUB_METHODS:-publickey,password,keyboard-interactive})." >&2
    exit 255 ;;
  *PreferredAuthentications=publickey*) [ "${STUB_PUBKEY_OK:-0}" = 1 ] ;;
  *) exit 99 ;;
esac
EOF
cat >"$bin/sshd" <<'EOF'
#!/usr/bin/env bash
[ "$1" = -t ] && exit "${STUB_SSHD_T_RC:-0}"
exit 0
EOF
cat >"$bin/plutil" <<'EOF'
#!/usr/bin/env bash
echo false
EOF
cat >"$bin/sudo" <<'EOF'
#!/usr/bin/env bash
echo "stub sudo must not be called: $*" >&2; exit 98
EOF
# id: the real one, except `ci` exists only once the stub sysadminctl made it.
cat >"$bin/id" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = ci ] || [ "\${2:-}" = ci ]; then
  [ -e "\$STUB_STATE/ci-exists" ] || exit 1
  echo 600; exit 0
fi
exec "$REAL_ID" "\$@"
EOF
cat >"$bin/dseditgroup" <<'EOF'
#!/usr/bin/env bash
[ -e "$STUB_STATE/ci-admin" ]
EOF
cat >"$bin/sysadminctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_STATE/sysadminctl.log"
touch "$STUB_STATE/ci-exists"
EOF
cat >"$bin/dscl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_STATE/dscl.log"
EOF
cat >"$bin/createhomedir" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$bin/openssl" <<'EOF'
#!/usr/bin/env bash
echo STUBSECRETPASSWORD
EOF
cat >"$bin/launchctl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$bin/ghapp" <<'EOF'
#!/usr/bin/env bash
echo shipyard,shipyard-macos
EOF
chmod +x "$bin"/*
export PATH="$bin:$PATH" FLEET_OPS_TEST=1
export STUB_STATE="$tmp/state"
mkdir -p "$STUB_STATE"

# ── sshd-pubkey-only.sh ─────────────────────────────────────────────────────
confd="$tmp/sshd_config.d"
home="$tmp/home"
mkdir -p "$confd" "$home/.ssh"
export FLEET_OPS_TEST_SSHD_CONFIG_D="$confd" FLEET_OPS_TEST_HOME="$home"
dropin="$confd/100-fleet-pubkey-only.conf"

expect_rc 2 "bad flag" -- bash "$SSHD_OPS" --bogus

# Plan: current methods, drop-in state, launch mode, guard preview.
expect_rc 0 "plan" -- env FLEET_OPS_TEST_EUID=501 bash "$SSHD_OPS"
has out "current login methods offered: publickey,password,keyboard-interactive"
has out "100-fleet-pubkey-only.conf: absent"
has out "one sshd per connection"
has out "--apply would refuse"
has out "PasswordAuthentication no"
[ ! -e "$dropin" ] || fail "plan wrote the drop-in"

# Not root: refused with the exact sudo command.
expect_rc 3 "apply as non-root" -- env FLEET_OPS_TEST_EUID=501 bash "$SSHD_OPS" --apply
has err "needs root; run: sudo $SSHD_OPS --apply"
expect_rc 3 "rollback as non-root" -- env FLEET_OPS_TEST_EUID=501 bash "$SSHD_OPS" --rollback
has err "run: sudo $SSHD_OPS --rollback"

# Root without SUDO_USER: cannot prove anyone's key login.
expect_rc 3 "apply without SUDO_USER" -- env -u SUDO_USER FLEET_OPS_TEST_EUID=0 bash "$SSHD_OPS" --apply
has err "SUDO_USER"

# Lockout guard: no authorized_keys.
expect_rc 3 "apply without authorized_keys" -- env FLEET_OPS_TEST_EUID=0 SUDO_USER="$me" bash "$SSHD_OPS" --apply
has err "no non-empty $home/.ssh/authorized_keys"
[ ! -e "$dropin" ] || fail "guard let the drop-in through (no keys)"

# Lockout guard: keys present but key login fails.
echo "ssh-ed25519 AAAAtest test" >"$home/.ssh/authorized_keys"
expect_rc 3 "apply with failing key login" -- env FLEET_OPS_TEST_EUID=0 SUDO_USER="$me" STUB_PUBKEY_OK=0 bash "$SSHD_OPS" --apply
has err "key-only ssh to localhost as $me failed"
[ ! -e "$dropin" ] || fail "guard let the drop-in through (key login failed)"

# sshd -t rejects: nothing left behind.
expect_rc 4 "apply with sshd -t failing" -- env FLEET_OPS_TEST_EUID=0 SUDO_USER="$me" STUB_PUBKEY_OK=1 STUB_SSHD_T_RC=1 bash "$SSHD_OPS" --apply
[ ! -e "$dropin" ] || fail "drop-in left after sshd -t failed"

# Guard passes (temp dir only): writes, re-probes, and is idempotent.
expect_rc 0 "apply" -- env FLEET_OPS_TEST_EUID=0 SUDO_USER="$me" STUB_PUBKEY_OK=1 STUB_METHODS=publickey bash "$SSHD_OPS" --apply
has out "wrote $dropin"
has out "login methods now:    publickey"
grep -qx "PasswordAuthentication no" "$dropin" || fail "drop-in lacks PasswordAuthentication no"
grep -qx "KbdInteractiveAuthentication no" "$dropin" || fail "drop-in lacks KbdInteractiveAuthentication no"
expect_rc 0 "apply again" -- env FLEET_OPS_TEST_EUID=0 SUDO_USER="$me" STUB_PUBKEY_OK=1 STUB_METHODS=publickey bash "$SSHD_OPS" --apply
has out "already current"
# A different existing drop-in is restored when sshd -t rejects the new one.
echo "PasswordAuthentication yes" >"$dropin"
expect_rc 4 "apply over a different drop-in, sshd -t failing" -- env FLEET_OPS_TEST_EUID=0 SUDO_USER="$me" STUB_PUBKEY_OK=1 STUB_SSHD_T_RC=1 bash "$SSHD_OPS" --apply
[ "$(cat "$dropin")" = "PasswordAuthentication yes" ] || fail "previous drop-in not restored"
[ -z "$(find "$confd" -name '.100-fleet*')" ] || fail "temp files left in $confd"

expect_rc 0 "rollback" -- env FLEET_OPS_TEST_EUID=0 bash "$SSHD_OPS" --rollback
[ ! -e "$dropin" ] || fail "rollback left the drop-in"
expect_rc 0 "rollback again" -- env FLEET_OPS_TEST_EUID=0 bash "$SSHD_OPS" --rollback
has out "nothing to roll back"

# ── create-ci-runner-user.sh ────────────────────────────────────────────────
rhome="$tmp/runner-home"
rdir="$rhome/actions-ci/Shipyard-test-01"
mkdir -p "$rdir" "$rhome/Library/LaunchAgents"
printf '\357\273\277{\n  "agentId": 1,\n  "agentName": "Shipyard-test-01",\n  "gitHubUrl": "https://github.com/example/Shipyard",\n  "workFolder": "_work"\n}\n' >"$rdir/.runner"
plist="$rhome/Library/LaunchAgents/actions.runner.example-Shipyard.Shipyard-test-01.plist"
: >"$plist"
printf '%s\n' "$plist" >"$rdir/.service"
mkdir -p "$rhome/actions-ci/cache"   # no .runner: not a runner
export FLEET_OPS_TEST_HOME="$rhome"

expect_rc 2 "ci: bad flag" -- bash "$CI_OPS" --bogus
expect_rc 0 "ci: help" -- bash "$CI_OPS" --help
has out "fleet SSH keys"

expect_rc 0 "ci: plan" -- env FLEET_OPS_TEST_EUID=501 bash "$CI_OPS"
has out "Shipyard-test-01"
has out "repo:        example/Shipyard"
has out "labels:      shipyard,shipyard-macos"
has out "actions.runner.example-Shipyard.Shipyard-test-01.plist (loaded)"
has out "user 'ci': absent"
has out "launchctl bootout gui/"
has out "actions/runners/remove-token"
has out "./config.sh remove --token"
has out "ghapp api -X POST repos/example/Shipyard/actions/runners/registration-token"
has out "--name Shipyard-test-01"
has out "--labels shipyard,shipyard-macos"
has out "<key>UserName</key><string>ci</string>"
lacks out "cache"
[ ! -e "$STUB_STATE/sysadminctl.log" ] || fail "plan ran sysadminctl"

expect_rc 3 "ci: apply as non-root" -- env FLEET_OPS_TEST_EUID=501 bash "$CI_OPS" --apply
has err "needs root; run: sudo $CI_OPS --apply"
[ ! -e "$STUB_STATE/sysadminctl.log" ] || fail "non-root apply ran sysadminctl"

# Root (stubbed): creates a hidden standard user, never prints the password.
expect_rc 0 "ci: apply" -- env FLEET_OPS_TEST_EUID=0 bash "$CI_OPS" --apply
has out "created standard user 'ci'"
lacks out STUBSECRETPASSWORD
lacks err STUBSECRETPASSWORD
grep -q -- "-addUser ci " "$STUB_STATE/sysadminctl.log" || fail "sysadminctl not asked to add ci"
! grep -q -- "-admin" "$STUB_STATE/sysadminctl.log" || fail "ci created as admin"
grep -qx ". -create /Users/ci IsHidden 1" "$STUB_STATE/dscl.log" || fail "ci not hidden"
has out "./config.sh --unattended"

# Idempotent: an existing standard ci is left alone.
rm -f "$STUB_STATE/sysadminctl.log"
expect_rc 0 "ci: apply again" -- env FLEET_OPS_TEST_EUID=0 bash "$CI_OPS" --apply
has out "already exists and is a standard user"
[ ! -e "$STUB_STATE/sysadminctl.log" ] || fail "second apply ran sysadminctl"

# An existing admin ci is refused.
touch "$STUB_STATE/ci-admin"
expect_rc 3 "ci: apply with admin ci" -- env FLEET_OPS_TEST_EUID=0 bash "$CI_OPS" --apply
has err "is an admin"

echo 'fleet ops scripts: ok'
