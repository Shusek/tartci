#!/usr/bin/env bash
# Opt-in guest isolation for the shared (non-prepared) Tart lanes.
#
# A guest runs whatever job GitHub assigns its runner, so on a host that also
# serves more trusted work it must not be able to:
#
#   * write the host build caches a later job consumes. With
#     TARTCI_HOST_CACHE_ACCESS=ro the ccache (and, on macOS, configure-check)
#     shares are mounted read-only and the guest's ccache runs read-only: a job
#     still gets warm hits but cannot plant objects that a main or release
#     build would link. Give each trust class its own TARTCI_CI_CACHE and let
#     only the trusted class run with the default `rw`.
#   * reach the host, the LAN or sibling VMs. With TARTCI_TART_NETWORK=softnet
#     Tart's Softnet isolates each guest; TARTCI_TART_SOFTNET_ALLOW lists the
#     CIDRs a guest may still reach (comma-separated, e.g. a host proxy).
#
# Both default to the historical behaviour (`rw`, `shared`).

TARTCI_HOST_CACHE_ACCESS_MODE=rw
TARTCI_TART_NETWORK_ARGS=()

# Validate the knobs once at startup. Prints the problem and returns 2.
tartci_guest_isolation_configure(){
  TARTCI_HOST_CACHE_ACCESS_MODE="${TARTCI_HOST_CACHE_ACCESS:-rw}"
  case "$TARTCI_HOST_CACHE_ACCESS_MODE" in
    rw|ro) ;;
    *) printf "invalid TARTCI_HOST_CACHE_ACCESS='%s' (rw|ro)\n" "$TARTCI_HOST_CACHE_ACCESS_MODE" >&2
       return 2 ;;
  esac
  TARTCI_TART_NETWORK_ARGS=()
  case "${TARTCI_TART_NETWORK:-shared}" in
    shared) ;;
    softnet)
      TARTCI_TART_NETWORK_ARGS=(--net-softnet)
      if [ -n "${TARTCI_TART_SOFTNET_ALLOW:-}" ]; then
        case "$TARTCI_TART_SOFTNET_ALLOW" in
          *[!0-9A-Fa-f.:/,]*)
            printf "invalid TARTCI_TART_SOFTNET_ALLOW='%s' (comma-separated CIDRs)\n" \
              "$TARTCI_TART_SOFTNET_ALLOW" >&2
            return 2 ;;
        esac
        TARTCI_TART_NETWORK_ARGS+=("--net-softnet-allow=$TARTCI_TART_SOFTNET_ALLOW")
      fi ;;
    *) printf "invalid TARTCI_TART_NETWORK='%s' (shared|softnet)\n" "${TARTCI_TART_NETWORK:-}" >&2
       return 2 ;;
  esac
}

# The `tart run --dir` suffix for a host cache share: ":ro" in read-only mode.
tartci_host_cache_mount_suffix(){
  [ "$TARTCI_HOST_CACHE_ACCESS_MODE" != ro ] || printf ':ro'
}
