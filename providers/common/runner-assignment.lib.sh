#!/usr/bin/env bash
# Shared bounded wait for an ephemeral Actions runner to claim its job.
#
# The caller owns the runner process and supplies an idempotent cleanup
# callback. Cleanup runs after every terminal path, including the unassigned
# timeout, so a race-loser JIT runner cannot retain a VM or capacity lease.

TARTCI_RUNNER_WAS_ASSIGNED=0

tartci_validate_bounded_positive_integer() {
  local name="$1" value="${2:-}" maximum="$3" normalized
  case "$value" in
    ''|*[!0-9]*)
      printf '%s must be a positive integer, got: %s\n' \
        "$name" "${value:-<empty>}" >&2
      return 2
      ;;
  esac
  normalized="$value"
  while [ "${#normalized}" -gt 1 ] && [ "${normalized#0}" != "$normalized" ]; do
    normalized="${normalized#0}"
  done
  if [ "$normalized" = 0 ]; then
    printf '%s must be a positive integer, got: %s\n' "$name" "$value" >&2
    return 2
  fi
  # Reject overflow before asking Bash to interpret the caller's input as an
  # integer. For equal-width digit strings, both operands fit because maximum
  # is a small source-owned limit.
  if [ "${#normalized}" -gt "${#maximum}" ] \
    || { [ "${#normalized}" -eq "${#maximum}" ] \
      && [ "$normalized" -gt "$maximum" ]; }; then
    printf '%s must be at most %s, got: %s\n' "$name" "$maximum" "$value" >&2
    return 2
  fi
}

tartci_validate_runner_idle_timeout() {
  tartci_validate_bounded_positive_integer \
    TARTCI_RUNNER_IDLE_TIMEOUT_SECS "${1:-}" 86400
}

# Host-side backstop for an assigned job, in seconds. GitHub's own default job
# timeout is six hours; without a host deadline a wedged or hostile job could
# keep its VM (and its share of the host) alive indefinitely.
TARTCI_RUNNER_JOB_TIMEOUT_DEFAULT=21600

_tartci_observe_runner_assignment() {
  local runner_log="$1" assigned_fn="${2:-}" assignment_line=""
  [ "$TARTCI_RUNNER_WAS_ASSIGNED" = 0 ] || return 0
  assignment_line="$(grep 'Running job:' "$runner_log" 2>/dev/null | tail -1)" \
    || return 0
  [ -n "$assignment_line" ] || return 0
  TARTCI_RUNNER_WAS_ASSIGNED=1
  printf 'TARTCI_DIAG runner_assignment=claimed %s\n' "$assignment_line" >&2
  [ -z "$assigned_fn" ] || "$assigned_fn" \
    || printf 'TARTCI_DIAG runner_assignment_state_update=failed\n' >&2
}

# Usage: tartci_monitor_runner_assignment PID LOG TIMEOUT CLEANUP [POLL] [ASSIGNED] [AUTHORITATIVE]
#
# Returns the runner's status, or 124 when it remained unassigned for TIMEOUT
# seconds. Once "Running job:" is observed, the assignment deadline is
# permanently disabled and the valid job may run for up to
# TARTCI_JOB_TIMEOUT_SECS (default six hours) before it is killed (also 124,
# with TARTCI_RUNNER_WAS_ASSIGNED=1).
tartci_monitor_runner_assignment() {
  local runner_pid="$1" runner_log="$2" timeout="$3" cleanup_fn="$4"
  local poll="${5:-5}" assigned_fn="${6:-}" authoritative_fn="${7:-}"
  local started now idle_elapsed rc=0 authoritative_rc=1 assigned_at=""
  local uncertainty_count=0 uncertainty_max="${TARTCI_RUNNER_ASSIGNMENT_VERIFY_ATTEMPTS:-3}"
  local job_timeout="${TARTCI_JOB_TIMEOUT_SECS:-$TARTCI_RUNNER_JOB_TIMEOUT_DEFAULT}"
  TARTCI_RUNNER_WAS_ASSIGNED=0
  tartci_validate_runner_idle_timeout "$timeout" || return $?
  tartci_validate_bounded_positive_integer \
    TARTCI_RUNNER_ASSIGNMENT_VERIFY_ATTEMPTS "$uncertainty_max" 20 || return $?
  tartci_validate_bounded_positive_integer \
    TARTCI_JOB_TIMEOUT_SECS "$job_timeout" 432000 || return $?
  if ! declare -F "$cleanup_fn" >/dev/null 2>&1; then
    printf 'runner assignment cleanup callback is unavailable: %s\n' \
      "$cleanup_fn" >&2
    return 2
  fi

  started="$(date +%s)"
  while kill -0 "$runner_pid" 2>/dev/null; do
    _tartci_observe_runner_assignment "$runner_log" "$assigned_fn"
    if [ "$TARTCI_RUNNER_WAS_ASSIGNED" = 1 ]; then
      now="$(date +%s)"
      [ -n "$assigned_at" ] || assigned_at="$now"
      if [ $((now - assigned_at)) -ge "$job_timeout" ]; then
        printf 'TARTCI_DIAG runner_job=timeout elapsed=%ss limit=%ss\n' \
          "$((now - assigned_at))" "$job_timeout" >&2
        kill -9 "$runner_pid" 2>/dev/null || true
        wait "$runner_pid" 2>/dev/null || true
        "$cleanup_fn"
        return 124
      fi
    fi
    if [ "$TARTCI_RUNNER_WAS_ASSIGNED" = 0 ]; then
      now="$(date +%s)"
      idle_elapsed=$((now - started))
      if [ "$idle_elapsed" -ge "$timeout" ]; then
        # Close the observation/kill race at the deadline: a valid job may have
        # emitted its marker after the loop's first read.
        _tartci_observe_runner_assignment "$runner_log" "$assigned_fn"
        [ "$TARTCI_RUNNER_WAS_ASSIGNED" = 1 ] && continue
        if [ -n "$authoritative_fn" ]; then
          if "$authoritative_fn"; then
            TARTCI_RUNNER_WAS_ASSIGNED=1
            printf 'TARTCI_DIAG runner_assignment=claimed authoritative=github_busy\n' >&2
            [ -z "$assigned_fn" ] || "$assigned_fn" \
              || printf 'TARTCI_DIAG runner_assignment_state_update=failed\n' >&2
            continue
          else
            authoritative_rc=$?
          fi
          if [ "$authoritative_rc" -ne 1 ]; then
            uncertainty_count=$((uncertainty_count + 1))
            printf 'TARTCI_DIAG runner_assignment=unknown authoritative_check_rc=%s\n' \
              "$authoritative_rc" >&2
            if [ "$uncertainty_count" -lt "$uncertainty_max" ]; then
              sleep "$poll"
              continue
            fi
            printf 'TARTCI_DIAG runner_assignment=unconfirmed retries_exhausted=%s\n' \
              "$uncertainty_count" >&2
            _tartci_observe_runner_assignment "$runner_log" "$assigned_fn"
            [ "$TARTCI_RUNNER_WAS_ASSIGNED" = 1 ] && continue
          fi
        fi
        # The authoritative request itself has latency. Close that observation
        # window before destroying a runner confirmed idle at the API snapshot.
        _tartci_observe_runner_assignment "$runner_log" "$assigned_fn"
        [ "$TARTCI_RUNNER_WAS_ASSIGNED" = 1 ] && continue
        printf 'TARTCI_DIAG runner_assignment=timeout elapsed=%ss limit=%ss\n' \
          "$idle_elapsed" "$timeout" >&2
        kill -9 "$runner_pid" 2>/dev/null || true
        wait "$runner_pid" 2>/dev/null || true
        "$cleanup_fn"
        return 124
      fi
    fi
    sleep "$poll"
  done

  # Preserve assignment truth even when a short job exits between polls.
  _tartci_observe_runner_assignment "$runner_log" "$assigned_fn"
  wait "$runner_pid" || rc=$?
  "$cleanup_fn"
  return "$rc"
}
