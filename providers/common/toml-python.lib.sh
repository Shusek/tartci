# shellcheck shell=bash
# The Python that can read TOML (tomllib, 3.11+), for tartci and for the
# provider scripts an operator runs directly.
#
# macOS still ships Python 3.9, but fleet profile/readiness helpers use the
# stdlib tomllib module added in Python 3.11. Resolve a supported interpreter
# explicitly so launchd cannot turn a real readiness result into the generic
# readiness_probe_failed fallback. TARTCI_PYTHON may name an absolute path or
# a PATH command; otherwise prefer common Homebrew 3.11/3.12 installations.
tartci_toml_python_path() {
  local candidate configured="${TARTCI_PYTHON:-}"
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
