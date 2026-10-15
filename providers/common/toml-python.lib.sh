# shellcheck shell=bash
# The Python that can read TOML (tomllib, 3.11+), for tartci and for the
# provider scripts an operator runs directly.
#
# macOS still ships Python 3.9, but fleet profile/readiness helpers use the
# stdlib tomllib module added in Python 3.11. Resolve a supported interpreter
# explicitly so launchd cannot turn a real readiness result into the generic
# readiness_probe_failed fallback. TARTCI_PYTHON may name an absolute path or
# a PATH command; otherwise prefer common Homebrew 3.11/3.12 installations.
# The host's pinned interpreter: the absolute path the fleet installer wrote
# to ~/.config/tartci/python.pin (TARTCI_PYTHON_PIN overrides the file). It is
# the same path the lane LaunchAgents carry as TARTCI_PYTHON, so a LaunchAgent
# rendered without it (the watchdog, reclaim, self-update) runs that same
# binary instead of whatever `python3` a Homebrew upgrade links next.
tartci_python_pin_file() {
  printf '%s\n' "${TARTCI_PYTHON_PIN:-$HOME/.config/tartci/python.pin}"
}

tartci_pinned_python() {
  local pin candidate
  pin="$(tartci_python_pin_file)"
  [ -f "$pin" ] || return 1
  candidate="$(head -n 1 "$pin")"
  case "$candidate" in /*) ;; *) return 1 ;; esac
  printf '%s\n' "$candidate"
}

# host.python from a fleet profile: one absolute path on a `python = "..."`
# line of the [host] table. Read without a TOML parser, which this is choosing.
tartci_profile_python() {
  [ -f "${1:-}" ] || return 1
  awk '
    /^[[:space:]]*\[/ { in_host = ($0 ~ /^[[:space:]]*\[host\][[:space:]]*$/); next }
    in_host && /^[[:space:]]*python[[:space:]]*=/ {
      if (match($0, /"\/[^"]+"/)) { print substr($0, RSTART + 1, RLENGTH - 2); found = 1; exit }
    }
    END { exit found ? 0 : 1 }
  ' "$1"
}

# A refusal must be seen, not only logged to a job's stderr: record the first
# one (time, job, reason) for the watchdog's interpreter check, which opens an
# issue. The installer clears it once a working pin is in place.
tartci_interpreter_refusal_file() {
  printf '%s\n' "${TARTCI_HOME:-$HOME/.tartci}/state/interpreter-refused"
}

tartci_note_interpreter_refusal() {
  local marker
  marker="$(tartci_interpreter_refusal_file)"
  [ -e "$marker" ] && return 0
  mkdir -p "$(dirname "$marker")" 2>/dev/null || return 0
  printf '%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${XPC_SERVICE_NAME:-interactive}" "$1" \
    >"$marker" 2>/dev/null || true
}

tartci_toml_python_path() {
  local candidate configured="${TARTCI_PYTHON:-}" pinned
  if [ -z "$configured" ] && pinned="$(tartci_pinned_python)"; then
    if [ -x "$pinned" ] && "$pinned" -c 'import tomllib' >/dev/null 2>&1; then
      printf '%s\n' "$pinned"
      return 0
    fi
    echo "tartci: the pinned interpreter in $(tartci_python_pin_file) is unusable: $pinned" \
         "(missing, or without tomllib); reinstall with a working host.python" >&2
    tartci_note_interpreter_refusal "pinned interpreter unusable: $pinned"
    return 127
  fi
  if [ -z "$configured" ]; then
    case "${XPC_SERVICE_NAME:-}" in
      com.danielraffel.tartci.*)
        # A LaunchAgent must never run whatever python3 PATH resolves to
        # today: a Homebrew upgrade would swap the binary under launchd, and
        # macOS privacy grants (Local Network) do not follow it.
        echo "tartci: refusing an unpinned interpreter under launchd job $XPC_SERVICE_NAME:" \
             "no TARTCI_PYTHON and no $(tartci_python_pin_file); set host.python in the" \
             "fleet profile and reinstall" >&2
        tartci_note_interpreter_refusal "no TARTCI_PYTHON and no $(tartci_python_pin_file)"
        return 127
        ;;
    esac
  fi
  if [ -n "$configured" ]; then
    candidate="$(command -v "$configured" 2>/dev/null || true)"
    [ -n "$candidate" ] && "$candidate" -c 'import tomllib' >/dev/null 2>&1 || {
      echo "tartci: TARTCI_PYTHON must name a Python 3.11+ interpreter with tomllib: $configured" >&2
      return 127
    }
    printf '%s\n' "$candidate"
    return 0
  fi

  for candidate in python3.12 python3.11; do
    candidate="$(command -v "$candidate" 2>/dev/null || true)"
    if [ -n "$candidate" ] && "$candidate" -c 'import tomllib' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  candidate="$(command -v python3 2>/dev/null || true)"
  if [ -n "$candidate" ] && "$candidate" -c 'import tomllib' >/dev/null 2>&1; then
    printf '%s\n' "$candidate"
    return 0
  fi

  for candidate in \
    /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
    /usr/local/bin/python3; do
    if [ -x "$candidate" ] && "$candidate" -c 'import tomllib' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "tartci: no Python 3.11+ interpreter with tomllib is available; set TARTCI_PYTHON" >&2
  return 127
}

tartci_toml_python() {
  local python
  python="$(tartci_toml_python_path)" || return 127
  "$python" "$@"
}

# Replace the current shell only when the caller is at a dispatcher tail.
# Mid-flow tartci helpers must return so pool-on and verification can continue.
tartci_toml_python_exec() {
  local python
  python="$(tartci_toml_python_path)" || return 127
  exec "$python" "$@"
}
