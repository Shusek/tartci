# shellcheck shell=bash
# A lane-owned cache root (fleet profile `cache_root`) must be the directory
# its path names. The profile validator compares paths as text; a symlink in
# the root, or in its ccache or configure-checks subdirectory, would make the
# VM mount whatever the link points at, the host-wide root that every PR gate
# VM writes included. TARTCI_HOST_CI_CACHE is set only for such a lane.

tartci_physical_dir() {
  (cd "$1" 2>/dev/null && pwd -P)
}

# Prints the reason and returns 1 when the lane root is unsafe to mount.
tartci_lane_cache_root_refusal() {
  local root="$1" host="${2:-}" dir physical root_physical host_physical
  [ -n "$host" ] || return 0
  for dir in "$root" "$root/ccache" "$root/configure-checks"; do
    # Not created yet is fine: the disk-root preparation creates it without
    # following links. A link, or an existing path that resolves elsewhere,
    # is not.
    if [ -L "$dir" ]; then
      printf 'lane cache root: %s is a symlink; it would mount another cache\n' "$dir"
      return 1
    fi
    [ -e "$dir" ] || continue
    if ! physical="$(tartci_physical_dir "$dir")"; then
      printf 'lane cache root: %s is not a directory\n' "$dir"
      return 1
    fi
    if [ "$physical" != "$dir" ]; then
      printf 'lane cache root: %s resolves to %s; a symlink would mount another cache\n' \
        "$dir" "$physical"
      return 1
    fi
  done
  root_physical="$(tartci_physical_dir "$root" || printf '%s' "$root")"
  host_physical="$(tartci_physical_dir "$host" || printf '%s' "$host")"
  case "$root_physical/" in
    "$host_physical/"*)
      printf 'lane cache root: %s is or sits inside the host cache root %s\n' \
        "$root_physical" "$host_physical"
      return 1 ;;
  esac
  case "$host_physical/" in
    "$root_physical/"*)
      printf 'lane cache root: %s contains the host cache root %s\n' \
        "$root_physical" "$host_physical"
      return 1 ;;
  esac
  return 0
}
