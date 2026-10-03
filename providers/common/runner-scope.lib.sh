#!/usr/bin/env bash
# Scope is independent of group ID: an organization's default group can be 1.
# "auto" preserves the historical macOS selection.
tartci_runner_api_root(){
  local repo="$1" group="$2" scope="$3" owner name
  case "$group" in
    ''|0*|*[!0-9]*) printf 'invalid TARTCI_RUNNER_GROUP_ID: expected a positive integer\n' >&2; return 2;;
  esac
  case "$repo" in
    ''|/*|*/|*/*/*) printf 'invalid runner repository: expected OWNER/REPO\n' >&2; return 2;;
    */*) owner="${repo%%/*}"; name="${repo#*/}";;
    *) printf 'invalid runner repository: expected OWNER/REPO\n' >&2; return 2;;
  esac
  case "$owner" in
    -*|*-|*[!A-Za-z0-9-]*) printf 'invalid runner repository: expected OWNER/REPO\n' >&2; return 2;;
  esac
  case "$name" in
    *[!A-Za-z0-9_.-]*) printf 'invalid runner repository: expected OWNER/REPO\n' >&2; return 2;;
  esac
  case "$scope" in
    auto) if [ "$group" = 1 ]; then scope=repo; else scope=org; fi;;
    repo|org) ;;
    *) printf 'invalid TARTCI_RUNNER_SCOPE: expected repo, org, or auto\n' >&2; return 2;;
  esac
  if [ "$scope" = repo ]; then printf 'repos/%s/actions/runners\n' "$repo"
  else printf 'orgs/%s/actions/runners\n' "$owner"; fi
}
