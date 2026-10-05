# Host VM-DHCP breaker for the macOS JIT supervisor (scripts/vm_dhcp_breaker.py).
# shellcheck shell=bash
# shellcheck disable=SC2034 # VM_DHCP_* state is read by runner.sh
#
# When the host's VM DHCP server stops answering, every boot waits 120 s for an
# address and is discarded. After two `no_ip` in a row within 15 min the host's
# breaker opens: no lane clones, except one probe every 300 s (or at once when
# bootpd's run counter moves), and the first address any VM gets closes it.
# This is checked before anything else a lane does to boot, before the job
# claim, so a lane that backs off here has published no claim.
#
# Fail open: any error reading or writing the breaker boots as before.
# TARTCI_VM_DHCP_BREAKER=0 (profile host `vm_dhcp_breaker = false`) disables
# it: no reads, no writes, no events.
VM_DHCP_BACKOFF=0
VM_DHCP_PROBE=0

tartci_vm_dhcp_enabled(){
  [ "${TARTCI_VM_DHCP_BREAKER:-1}" = 1 ]
}

tartci_vm_dhcp_validate(){
  case "${TARTCI_VM_DHCP_BREAKER:-1}" in 0|1) ;; *)
    printf 'invalid TARTCI_VM_DHCP_BREAKER: expected 0 or 1\n' >&2; return 2 ;;
  esac
}

# Emit the events a breaker call returned (one per line: name<TAB>detail).
_tartci_vm_dhcp_emit(){
  local name detail
  while IFS=$'\t' read -r name detail; do
    [ -n "$name" ] && event "$name" "$detail"
  done < <(printf '%s' "$1" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
except ValueError:
    raise SystemExit
for name, detail in value.get("events") or []:
    print("%s\t%s" % (name, detail))
' 2>/dev/null)
}

# 0: clone (closed, a probe granted, or the breaker could not be read).
# 75: the breaker is open and this lane is not the probe; an idle pass.
tartci_vm_dhcp_check(){
  local lane="$1" out action
  VM_DHCP_BACKOFF=0
  VM_DHCP_PROBE=0
  tartci_vm_dhcp_enabled || return 0
  out="$(python3 "$TARTCI_ROOT/scripts/vm_dhcp_breaker.py" check --lane "$lane" 2>/dev/null)" || return 0
  _tartci_vm_dhcp_emit "$out"
  action="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("action",""))' 2>/dev/null)"
  case "$action" in
    backoff) VM_DHCP_BACKOFF=1; return 75 ;;
    probe) VM_DHCP_PROBE=1; return 0 ;;
    *) return 0 ;;
  esac
}

# Record a boot's DHCP outcome: ip or no_ip.
tartci_vm_dhcp_record(){
  local lane="$1" outcome="$2" vm="${3:-}" out
  tartci_vm_dhcp_enabled || return 0
  out="$(python3 "$TARTCI_ROOT/scripts/vm_dhcp_breaker.py" record --outcome "$outcome" \
    --lane "$lane" --vm "$vm" 2>/dev/null)" || return 0
  _tartci_vm_dhcp_emit "$out"
}
