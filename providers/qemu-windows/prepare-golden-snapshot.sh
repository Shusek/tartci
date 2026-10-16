#!/usr/bin/env bash
# Make a private APFS CoW backing file and prove its exact bytes before boot.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SOURCE="${1:?source qcow2 required}"
SNAPSHOT="${2:?snapshot qcow2 required}"
# shellcheck source=providers/common/toml-python.lib.sh
source "$ROOT/providers/common/toml-python.lib.sh"
# The caller passes its pinned interpreter as TARTCI_PYTHON; without one this
# resolves the host pin, and under launchd refuses rather than use PATH.
PYTHON_BIN="$(tartci_toml_python_path)" || {
  echo "no pinned interpreter to hash the Windows snapshot with" >&2
  exit 127
}
[[ ! -e "$SNAPSHOT" && ! -L "$SNAPSHOT" ]] || {
  echo "Windows snapshot already exists: $SNAPSHOT" >&2
  exit 1
}
[[ -f "$SOURCE" && ! -L "$SOURCE" ]] || {
  echo "Windows golden must be a regular file: $SOURCE" >&2
  exit 1
}
[[ -f "$SOURCE.sha256" && ! -L "$SOURCE.sha256" ]] || {
  echo "Windows golden SHA-256 sidecar missing or linked: $SOURCE.sha256" >&2
  exit 1
}
snapshot_ready=0
cleanup_partial_snapshot(){
  [ "$snapshot_ready" = 0 ] || return 0
  chflags nouchg "$SNAPSHOT" "$SNAPSHOT.sha256" 2>/dev/null || true
  [ ! -e "$SNAPSHOT" ] || rm "$SNAPSHOT"
  [ ! -e "$SNAPSHOT.sha256" ] || rm "$SNAPSHOT.sha256"
}
trap cleanup_partial_snapshot EXIT

# cp -c fails closed off APFS or across volumes. A regular copy would consume
# another full Windows disk for every job and must be admitted separately.
cp -c "$SOURCE" "$SNAPSHOT"
cp "$SOURCE.sha256" "$SNAPSHOT.sha256"
cmp -s "$SOURCE.sha256" "$SNAPSHOT.sha256" || {
  echo "Windows golden sidecar changed while cloning" >&2
  exit 1
}
[[ -f "$SNAPSHOT" && ! -L "$SNAPSHOT" && -f "$SNAPSHOT.sha256" && ! -L "$SNAPSHOT.sha256" ]] || {
  echo "Windows snapshot is not a regular file" >&2
  exit 1
}
chflags uchg "$SNAPSHOT" "$SNAPSHOT.sha256"
# Every job hashes the whole snapshot (about 26 GB): log the cost so it is
# visible when deciding whether the immutable clone alone would do.
hash_started="$(date +%s)"
nice -n 20 "$PYTHON_BIN" "$ROOT/scripts/verify_windows_golden.py" --no-cache "$SNAPSHOT"
echo "Windows snapshot hash: $(( $(date +%s) - hash_started ))s for $(wc -c <"$SNAPSHOT" | tr -d ' ') bytes ($PYTHON_BIN)" >&2
snapshot_ready=1
