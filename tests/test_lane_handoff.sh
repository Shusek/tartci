#!/usr/bin/env bash
# One supervisor per lane: a replacement asks the incumbent to hand off and
# waits; the incumbent steps aside between jobs; a stale or reused claim is not
# an incumbent. Real processes, the functions as runner.sh ships them.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/tartci-handoff-test.XXXXXX")" && pwd)"
pids=""
cleanup() { for p in $pids; do kill "$p" 2>/dev/null || true; done; rm -rf "$tmp"; }
trap cleanup EXIT
fns="$tmp/fns.sh"
for name in tartci_proc_start tartci_lane_claim_path tartci_lane_handoff_path \
            tartci_lane_other_supervisor tartci_lane_claim tartci_lane_handoff_requested; do
  awk -v n="$name" '
    $0 ~ "^" n "\\(\\)\\{" { on = 1; print; if ($0 ~ /\}$/) exit; next }
    on { print; if ($0 ~ /^\}/) exit }' "$ROOT/providers/tart-macos/runner.sh" >>"$fns"
  grep -q "^$name()" "$fns" || { echo "missing $name in runner.sh" >&2; exit 1; }
done
cat >>"$fns" <<EOF
STATE_DIR="$tmp/state"; RUNNER_NAME=lane-1; POLL=1
TARTCI_LANE_HANDOFF_POLL=1 TARTCI_LANE_HANDOFF_MAX_WAIT=20
event() { printf '%s %s\n' "\$1" "\$2" >>"$tmp/events"; }
note() { :; }
heartbeat() { :; }
EOF
claim="$tmp/state/lane-1.supervisor"
handoff="$tmp/state/lane-1.handoff"
# Under set -e a failing `! cmd` does not exit, so negative checks use refute.
refute() { if "$@"; then echo "unexpected: $*" >&2; exit 1; fi; }
wait_for() { for _ in $(seq 1 100); do eval "$1" && return 0; sleep 0.1; done; return 1; }

# (a) No incumbent: the lane is claimed at once, with no handoff.
"$BASH" -c "source '$fns'; tartci_lane_claim; echo \$\$ >'$tmp/a.pid'"
test "$(cut -d' ' -f1 "$claim")" = "$(cat "$tmp/a.pid")"
test ! -e "$handoff"
refute grep -q supervisor_handoff_wait "$tmp/events" 2>/dev/null

# (b) A live incumbent hands off: it claims, then waits for a request.
"$BASH" -c "source '$fns'; tartci_lane_claim
         until tartci_lane_handoff_requested; do sleep 0.1; done
         echo stepped >'$tmp/incumbent'" &
incumbent=$!; pids="$pids $incumbent"
wait_for "[ \"\$(cut -d' ' -f1 '$claim' 2>/dev/null)\" = $incumbent ]"
"$BASH" -c "source '$fns'; tartci_lane_claim; echo \$\$ >'$tmp/b.pid'" &
replacement=$!; pids="$pids $replacement"
wait "$replacement"
test "$(cat "$tmp/incumbent")" = stepped
test "$(cut -d' ' -f1 "$claim")" = "$(cat "$tmp/b.pid")"
test ! -e "$handoff"
grep -q "supervisor_handoff_wait incumbent=$incumbent" "$tmp/events"
grep -q "supervisor_handoff_done incumbent=$incumbent" "$tmp/events"

# (c) A claim left by a dead supervisor is taken without waiting.
printf '%s %s\n' 999999 "Mon Sep  1 00:00:00 2026" >"$claim"
: >"$tmp/events"
"$BASH" -c "source '$fns'; tartci_lane_claim"
refute grep -q supervisor_handoff_wait "$tmp/events"

# (d) A live pid whose start time is not the claim's (pid reuse) is not an
# incumbent either.
sleep 30 & other=$!; disown "$other"; pids="$pids $other"
printf '%s %s\n' "$other" "Mon Sep  1 00:00:00 2026" >"$claim"
"$BASH" -c "source '$fns'; tartci_lane_claim"
refute grep -q supervisor_handoff_wait "$tmp/events"

# The loop steps aside only between jobs, for either reason.
grep -A1 'if \[ -z "\$CURRENT_VM" \]; then' "$ROOT/providers/tart-macos/runner.sh" \
  | grep -q 'step_aside=""'
grep -q 'elif tartci_lane_handoff_requested; then' "$ROOT/providers/tart-macos/runner.sh"
grep -q '^  tartci_lane_claim$' "$ROOT/providers/tart-macos/runner.sh"
echo 'lane handoff: ok'
