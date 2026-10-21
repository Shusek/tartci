# Bounded storage admission guard. It only prevents NEW VM admission; it never
# touches an active VM, Tart, mounts, or the host. Exit 0 healthy, 10 warming
# degraded, 20 quarantined/unknown.
# shellcheck shell=bash

tartci_storage_health_check(){
  local path="${1:-$TART_HOME}" timeout="${TARTCI_STORAGE_HEALTH_TIMEOUT_SECS:-5}" failures="${TARTCI_STORAGE_HEALTH_FAILURE_THRESHOLD:-2}" recovery="${TARTCI_STORAGE_HEALTH_RECOVERY_THRESHOLD:-3}" output rc
  local -a state_args=()
  [ -z "${TARTCI_STORAGE_HEALTH_STATE:-}" ] || state_args=(--state "$TARTCI_STORAGE_HEALTH_STATE")
  output="$(python3 "$TARTCI_ROOT/scripts/storage_health.py" --path "$path" "${state_args[@]}" --timeout "$timeout" --failure-threshold "$failures" --recovery-threshold "$recovery" 2>&1)" || rc=$?
  rc="${rc:-0}"
  printf '%s\n' "$output"
  case "$rc" in 0|10|20) return "$rc" ;; *) return 20 ;; esac
}
