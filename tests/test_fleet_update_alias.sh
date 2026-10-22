#!/usr/bin/env bash
# tartci fleet update is fleet-macos self-update under a shorter name.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tartci="$ROOT/tartci"

# The same parser answers, so every option is passed through unchanged.
test "$(bash "$tartci" fleet update --help 2>&1)" = \
     "$(bash "$tartci" fleet-macos self-update --help 2>&1)"

# Bare `fleet` prints its usage; anything else is a usage error.
bash "$tartci" fleet | grep -q 'tartci fleet update'
set +e
bash "$tartci" fleet bogus >/dev/null 2>&1
rc=$?
set -e
test "$rc" -eq 2
echo 'fleet update alias: ok'
