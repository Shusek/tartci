#!/usr/bin/env bash
# Create the non-admin `ci` account that the persistent Shipyard GitHub
# runners on a fleet Mac should run as, and print the steps that move each
# runner onto it.
#
#   fleet/ops/create-ci-runner-user.sh              # plan (default): runners found, steps
#   sudo fleet/ops/create-ci-runner-user.sh --apply # create the `ci` user, print the moves
#
# Why: the persistent runners (~/actions-ci/<name>/, one LaunchAgent each)
# run as the login account. A pull-request job then shares a uid with
# everything that account can read: the fleet SSH keys, the GitHub App
# private key, signing identities and notarization credentials. A job runs
# code from the PR, so any of that is one `cat` away. Running the runners as
# a separate standard user with no SSH keys and no keychain secrets puts that
# material behind a uid boundary.
#
# --apply does exactly one privileged thing: create the standard (non-admin)
# user `ci` with sysadminctl, a random password that is never printed or
# stored, hidden from the login window. It writes no SSH keys and no keychain
# items. It does NOT touch any runner: the per-runner re-registration
# (stop the old service, `config.sh remove`, `config.sh` as ci with a fresh
# token and the same labels, a LaunchDaemon with UserName=ci) is printed with
# exact commands for an operator to run one runner at a time.
#
# Idempotent: an existing `ci` user is checked (exists, not an admin) and
# left alone.
#
# Exit: 0 done/plan, 2 usage, 3 refused (not root, `ci` exists but is admin),
#       4 user creation failed.
#
# Test mode (FLEET_OPS_TEST=1 only): FLEET_OPS_TEST_EUID replaces the
# effective uid, FLEET_OPS_TEST_HOME the home searched for runners, and
# id/dscl/dseditgroup/sysadminctl/createhomedir/launchctl/openssl/ghapp are
# taken from PATH instead of their absolute paths.
set -euo pipefail

PROG="create-ci-runner-user"
CI_USER=ci
CI_HOME="/Users/$CI_USER"
SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

EUID_NOW="$(id -u)"
HOME_OVERRIDE=""
ID=/usr/bin/id
DSCL=/usr/bin/dscl
DSEDITGROUP=/usr/sbin/dseditgroup
SYSADMINCTL=/usr/sbin/sysadminctl
CREATEHOMEDIR=/usr/sbin/createhomedir
LAUNCHCTL=/bin/launchctl
OPENSSL=/usr/bin/openssl
if [ "${FLEET_OPS_TEST:-}" = 1 ]; then
  EUID_NOW="${FLEET_OPS_TEST_EUID:-$EUID_NOW}"
  HOME_OVERRIDE="${FLEET_OPS_TEST_HOME:-}"
  ID=id DSCL=dscl DSEDITGROUP=dseditgroup SYSADMINCTL=sysadminctl
  CREATEHOMEDIR=createhomedir LAUNCHCTL=launchctl OPENSSL=openssl
fi

usage() {
  cat <<EOF
usage: $PROG.sh [--plan | --apply]

  --plan    (default) list the persistent runners under ~/actions-ci/*/ and the steps
  --apply   root: create the standard user '$CI_USER' (hidden, no SSH keys, no
            keychain secrets), then print the per-runner re-registration commands

Why: pull-request jobs run code from the PR. On a runner that runs as the login
account, that code shares a uid with the fleet SSH keys, the GitHub App private
key and the signing/notarization material, and can read all of it. Moving the
runners to a separate non-admin '$CI_USER' account puts that material behind a
uid boundary. Only the user creation is automated; re-registering each runner
is printed for an operator to run, one runner at a time.

Run --apply with: sudo $SCRIPT_PATH --apply
EOF
}

MODE=plan
case "${1:-}" in
  ""|--plan) ;;
  --apply) MODE=apply ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { usage >&2; exit 2; }

say() { printf '%s: %s\n' "$PROG" "$*"; }
refuse() { printf '%s: refused: %s\n' "$PROG" "$*" >&2; exit 3; }

# The account that owns the runners today: whoever ran sudo, else ourselves.
owner_user() {
  if [ "$EUID_NOW" = 0 ] && [ -n "${SUDO_USER:-}" ]; then printf '%s\n' "$SUDO_USER"
  else "$ID" -un; fi
}

owner_home() {
  local u="$1" h=""
  if [ -n "$HOME_OVERRIDE" ]; then printf '%s\n' "$HOME_OVERRIDE"; return; fi
  h="$("$DSCL" . -read "/Users/$u" NFSHomeDirectory 2>/dev/null | sed -n 's/^NFSHomeDirectory: //p' || true)"
  printf '%s\n' "${h:-$HOME}"
}

# A field from a runner's .runner JSON (it starts with a UTF-8 BOM; sed does not care).
runner_field() {
  sed -n "s/.*\"$2\": *\"\\([^\"]*\\)\".*/\\1/p" "$1/.runner" | head -1
}

# The launchd label of a runner's service: its .service file names the plist;
# otherwise look for the conventional plist name.
runner_service_plist() {
  local dir="$1" home="$2" name="$3" f
  if [ -s "$dir/.service" ]; then head -1 "$dir/.service"; return; fi
  for f in "$home"/Library/LaunchAgents/actions.runner.*."$name".plist; do
    [ -e "$f" ] && { printf '%s\n' "$f"; return; }
  done
  echo ""
}

# Custom labels of a registered runner, comma-separated, via the GitHub App.
runner_labels() {
  local slug="$1" name="$2"
  command -v ghapp >/dev/null 2>&1 || { echo "unknown (ghapp not on PATH)"; return; }
  [ "$EUID_NOW" != 0 ] || { echo "unknown (run --plan without sudo to read labels)"; return; }
  ghapp api "repos/$slug/actions/runners?per_page=100" \
    --jq ".runners[] | select(.name == \"$name\") | [.labels[] | select(.type == \"custom\") | .name] | join(\",\")" \
    2>/dev/null || echo "unknown (ghapp api failed)"
}

ci_user_exists() { "$ID" "$CI_USER" >/dev/null 2>&1; }
ci_user_is_admin() { "$DSEDITGROUP" -o checkmember -m "$CI_USER" admin >/dev/null 2>&1; }

OWNER="$(owner_user)"
OWNER_HOME="$(owner_home "$OWNER")"
OWNER_UID="$("$ID" -u "$OWNER" 2>/dev/null || echo "<uid of $OWNER>")"

# Runner dirs: ~/actions-ci/<name>/ holding a .runner registration.
RUNNERS=()
for d in "$OWNER_HOME"/actions-ci/*/; do
  [ -f "$d.runner" ] && RUNNERS+=("${d%/}")
done

describe_runners() {
  local dir name url slug plist labels loaded
  say "persistent runners under $OWNER_HOME/actions-ci/ (owner $OWNER):"
  if [ "${#RUNNERS[@]}" -eq 0 ]; then
    echo "  (none found)"
    return
  fi
  for dir in ${RUNNERS[@]+"${RUNNERS[@]}"}; do
    name="$(runner_field "$dir" agentName)"
    url="$(runner_field "$dir" gitHubUrl)"
    slug="${url#https://github.com/}"
    plist="$(runner_service_plist "$dir" "$OWNER_HOME" "$name")"
    labels="$(runner_labels "$slug" "$name")"
    loaded=unknown
    if [ -n "$plist" ]; then
      if "$LAUNCHCTL" print "gui/$OWNER_UID/$(basename "$plist" .plist)" >/dev/null 2>&1; then
        loaded=loaded; else loaded="not loaded"; fi
    fi
    echo "  - $name"
    echo "      dir:         $dir"
    echo "      repo:        $slug"
    echo "      labels:      ${labels:-none (custom)}"
    echo "      LaunchAgent: ${plist:-none found} ($loaded)"
  done
}

ci_user_state() {
  if ! ci_user_exists; then echo absent
  elif ci_user_is_admin; then echo "present, ADMIN"
  else echo "present, standard"
  fi
}

# Exact commands to move one runner from the login account to `ci`.
print_moves() {
  local dir name url slug plist label labels labels_arg new daemon n=0
  if [ "${#RUNNERS[@]}" -eq 0 ]; then
    say "no runners to move"
    return
  fi
  cat <<EOF

$PROG: per-runner re-registration (NOT performed; run one runner at a time,
$PROG: as $OWNER on this host, and check each comes back online before the next).
$PROG: Toolchains the old runner found under $OWNER_HOME (rustup, cargo, uv, ...)
$PROG: are not visible to $CI_USER; install them for $CI_USER or system-wide first.
EOF
  for dir in ${RUNNERS[@]+"${RUNNERS[@]}"}; do
    n=$((n + 1))
    name="$(runner_field "$dir" agentName)"
    url="$(runner_field "$dir" gitHubUrl)"
    slug="${url#https://github.com/}"
    plist="$(runner_service_plist "$dir" "$OWNER_HOME" "$name")"
    label="$(basename "${plist:-actions.runner.${slug%%/*}-${slug#*/}.$name.plist}" .plist)"
    labels="$(runner_labels "$slug" "$name")"
    case "$labels" in
      unknown*) labels_arg=" --labels <same custom labels as today: $labels>" ;;
      "") labels_arg="" ;;
      *) labels_arg=" --labels $labels" ;;
    esac
    new="$CI_HOME/actions-ci/$name"
    daemon="/Library/LaunchDaemons/$label.plist"
    cat <<EOF

# ── runner $n: $name ($slug) ───────────────────────────────────────────────
# 1. stop the old LaunchAgent (no new jobs start; let a running job finish first)
launchctl bootout gui/$OWNER_UID/$label
# 2. remove the old registration
REMOVE_TOKEN="\$(ghapp api -X POST repos/$slug/actions/runners/remove-token --jq .token)"
(cd '$dir' && ./config.sh remove --token "\$REMOVE_TOKEN")
mv '${plist:-$OWNER_HOME/Library/LaunchAgents/$label.plist}' '${plist:-$OWNER_HOME/Library/LaunchAgents/$label.plist}.disabled'
# 3. copy the runner binaries (no credentials, env or work) to $CI_USER
sudo mkdir -p '$new'
sudo rsync -a --exclude '.credentials*' --exclude '.runner*' --exclude .service \\
  --exclude .env --exclude .path --exclude _work --exclude _diag '$dir/' '$new/'
sudo chown -R $CI_USER:staff '$CI_HOME/actions-ci'
# 4. register under $CI_USER with a fresh token and the SAME labels
REG_TOKEN="\$(ghapp api -X POST repos/$slug/actions/runners/registration-token --jq .token)"
sudo -u $CI_USER -H env REG_TOKEN="\$REG_TOKEN" sh -c 'cd "$new" && ./config.sh --unattended \\
  --url https://github.com/$slug --token "\$REG_TOKEN" --name $name \\
  --work _work$labels_arg --disableupdate --replace'
# 5. run it as a LaunchDaemon with UserName=$CI_USER ($CI_USER never logs in, so it has no gui domain)
sudo tee '$daemon' >/dev/null <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>UserName</key><string>$CI_USER</string>
  <key>ProgramArguments</key><array><string>$new/runsvc.sh</string></array>
  <key>WorkingDirectory</key><string>$new</string>
  <key>EnvironmentVariables</key><dict><key>ACTIONS_RUNNER_SVC</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>SessionCreate</key><true/>
  <key>StandardOutPath</key><string>$new/_diag/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$new/_diag/launchd.err.log</string>
</dict>
</plist>
PLIST
sudo mkdir -p '$new/_diag' && sudo chown $CI_USER:staff '$new/_diag'
sudo chown root:wheel '$daemon' && sudo chmod 644 '$daemon'
sudo launchctl bootstrap system '$daemon'
# 6. verify: online with the same labels, and the listener runs as $CI_USER
ghapp api 'repos/$slug/actions/runners?per_page=100' --jq '.runners[] | select(.name == "$name") | {status, labels: [.labels[].name]}'
ps -o user=,pid=,command= -U $CI_USER | grep Runner.Listener
EOF
  done
}

case "$MODE" in
  plan)
    describe_runners
    say "user '$CI_USER': $(ci_user_state)"
    cat <<EOF
$PROG: --apply would:
  1. create the standard (non-admin) user '$CI_USER' with home $CI_HOME and a random
     password that is never printed or stored; hide it from the login window
  2. write no SSH keys and no keychain items for it
  3. print, not run, the per-runner re-registration commands below
$PROG: run: sudo $SCRIPT_PATH --apply
EOF
    print_moves
    ;;

  apply)
    [ "$EUID_NOW" = 0 ] || refuse "--apply needs root; run: sudo $SCRIPT_PATH --apply"
    if ci_user_exists; then
      ci_user_is_admin && refuse "user '$CI_USER' exists and is an admin; runners must not run as an admin. Remove it from admin (sudo dseditgroup -o edit -d $CI_USER -t user admin) and re-run"
      say "user '$CI_USER' already exists and is a standard user; leaving it"
    else
      # The password is only on sysadminctl's argv for the life of that call;
      # nobody needs it: the runners start from launchd, never by login.
      pw="$("$OPENSSL" rand -base64 33 | tr -d '\n')"
      if ! "$SYSADMINCTL" -addUser "$CI_USER" -fullName "CI Runner" -home "$CI_HOME" \
          -shell /bin/zsh -password "$pw" >/dev/null 2>&1; then
        pw=""
        say "sysadminctl -addUser $CI_USER failed" >&2
        exit 4
      fi
      pw=""
      "$CREATEHOMEDIR" -c -u "$CI_USER" >/dev/null 2>&1 || true
      "$DSCL" . -create "/Users/$CI_USER" IsHidden 1
      ci_user_exists || { say "user '$CI_USER' missing after sysadminctl" >&2; exit 4; }
      ci_user_is_admin && { say "user '$CI_USER' came out an admin; investigate before using it" >&2; exit 4; }
      say "created standard user '$CI_USER' (hidden, home $CI_HOME, no SSH keys, no keychain secrets)"
    fi
    print_moves
    ;;
esac
