#!/usr/bin/env bash
# Populate the host artifact cache that macOS guests mount read-only.
#
#   scripts/artifact-cache.sh add --url <url> --sha256 <hex> [--dir DIR]
#   scripts/artifact-cache.sh git-sync --repo <owner/repo> [--branch main] [--dir DIR]
#   scripts/artifact-cache.sh prune --older-than-days <N> [--dir DIR]
#   scripts/artifact-cache.sh status [--dir DIR]
#
# add      downloads <url>, checks it against <hex> and stores it as
#          sha256/<hex>. The digest is the one the consuming job already pins,
#          so the cache can only ever hold bytes that job trusts. Re-adding an
#          existing blob re-verifies it and refreshes its age for prune.
# git-sync keeps git/<owner>/<repo>.git, a bare mirror of one branch, current.
#          Jobs use it as a Git alternate, so a stale mirror still saves every
#          byte it holds and the job fetches only what is newer.
# prune    removes blobs not added or re-added in the last <N> days. Mirrors
#          are bounded by their repository and are never pruned.
#
# A running guest may have the directory mounted, so nothing is ever rewritten
# in place: a blob lands in a private staging file on the same filesystem and
# is renamed into place, which is atomic, and a mirror only ever gains packs
# (a repack replaces packs the way `git gc` does in a live repository, which
# concurrent readers survive). Runners pick the cache up at the next VM boot;
# no service restart is needed.
set -euo pipefail

usage(){
  sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

die(){ printf 'artifact-cache: %s\n' "$*" >&2; exit 1; }

cmd="${1:-}"
case "$cmd" in
  add|git-sync|prune|status) shift;;
  -h|--help) usage 0;;
  *) usage 2;;
esac

cache_root="${TARTCI_CI_CACHE:-${PULP_CI_CACHE:-$HOME/.cache/pulp-ci}}"
dir="${TARTCI_ARTIFACT_CACHE_DIR:-$cache_root/artifact-cache}"
url="" sha="" repo="" branch="main" days=""
# Where mirrors fetch from; tests point it at a local repository.
git_base="${TARTCI_ARTIFACT_CACHE_GIT_BASE:-https://github.com}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --url) url="${2:-}"; shift 2;;
    --sha256) sha="${2:-}"; shift 2;;
    --repo) repo="${2:-}"; shift 2;;
    --branch) branch="${2:-}"; shift 2;;
    --older-than-days) days="${2:-}"; shift 2;;
    --dir) dir="${2:-}"; shift 2;;
    -h|--help) usage 0;;
    *) die "unknown argument: $1";;
  esac
done

case "$dir" in
  /*) ;;
  *) die "--dir must be an absolute path: $dir";;
esac
case "$dir" in
  *:*|*$'\n'*|*$'\r'*) die "--dir contains a character Tart cannot share: $dir";;
esac

sha256_of(){ shasum -a 256 "$1" | awk '{print $1}'; }

# One writer at a time per cache; a second sync waits rather than racing.
staging=""
lock_dir=""
cleanup(){
  [ -z "$staging" ] || rm -rf "$staging"
  [ -z "$lock_dir" ] || rmdir "$lock_dir" 2>/dev/null || true
}
lock_cache(){
  local waited=0
  mkdir -p "$dir"
  until mkdir "$dir/.lock" 2>/dev/null; do
    waited=$((waited + 1))
    [ "$waited" -le 600 ] || die "another sync has held $dir/.lock for 10 minutes"
    sleep 1
  done
  lock_dir="$dir/.lock"
  trap cleanup EXIT
}

case "$cmd" in
  add)
    [ -n "$url" ] || die "--url is required"
    [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || die "--sha256 must be 64 lowercase hex digits"
    case "$url" in
      https://*) ;;
      *) die "--url must be https: $url";;
    esac
    lock_cache
    mkdir -p "$dir/sha256"
    dest="$dir/sha256/$sha"
    if [ -f "$dest" ]; then
      [ "$(sha256_of "$dest")" = "$sha" ] \
        || die "$dest does not hash to its name; remove it by hand and re-add"
      touch "$dest"
      printf 'artifact-cache: sha256/%s already present (verified, age refreshed)\n' "$sha"
      exit 0
    fi
    staging="$(mktemp "$dir/sha256/.staging.XXXXXX")"
    curl --fail --location --silent --show-error --retry 5 --retry-all-errors \
      --retry-delay 5 --connect-timeout 30 --output "$staging" "$url" \
      || die "download failed: $url"
    actual="$(sha256_of "$staging")"
    [ "$actual" = "$sha" ] || die "SHA-256 mismatch for $url: expected $sha, got $actual"
    chmod 0644 "$staging"
    mv "$staging" "$dest"
    staging=""
    printf 'artifact-cache: added sha256/%s (%s bytes) from %s\n' \
      "$sha" "$(wc -c <"$dest" | tr -d ' ')" "$url"
    ;;

  git-sync)
    [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "--repo must look like owner/name"
    [[ "$branch" =~ ^[A-Za-z0-9_./-]+$ ]] || die "--branch has unsupported characters: $branch"
    lock_cache
    mirror="$dir/git/$repo.git"
    if [ ! -d "$mirror" ]; then
      mkdir -p "${mirror%/*}"
      staging="$(mktemp -d "${mirror%/*}/.staging.XXXXXX")"
      git init --quiet --bare "$staging"
      git -C "$staging" remote add origin "$git_base/$repo.git"
      # Never collect garbage on its own: a guest may be reading any pack.
      git -C "$staging" config gc.auto 0
      # Keep every fetch as a pack; loose objects would make a repack prune
      # the very files a guest is reading.
      git -C "$staging" config transfer.unpackLimit 1
      git -C "$staging" fetch --quiet --no-tags origin \
        "+refs/heads/$branch:refs/heads/$branch" \
        || die "initial fetch of $repo failed"
      mv "$staging" "$mirror"
      staging=""
    else
      git -C "$mirror" fetch --quiet --no-tags origin \
        "+refs/heads/$branch:refs/heads/$branch" \
        || die "fetch of $repo failed"
    fi
    # Each sync adds one pack. Fold them once they pile up; -d drops the old
    # packs only after the new one is complete, as `git gc` does.
    packs="$(find "$mirror/objects/pack" -name '*.pack' | wc -l | tr -d ' ')"
    if [ "$packs" -gt 16 ]; then
      git -C "$mirror" repack -a -d -q
    fi
    printf 'artifact-cache: %s at %s (%s)\n' "$repo" \
      "$(git -C "$mirror" rev-parse --short "refs/heads/$branch")" \
      "$(du -sh "$mirror" | awk '{print $1}')"
    ;;

  prune)
    [[ "$days" =~ ^[1-9][0-9]*$ ]] || die "--older-than-days must be a positive integer"
    [ -d "$dir/sha256" ] || { printf 'artifact-cache: nothing to prune\n'; exit 0; }
    lock_cache
    removed=0
    while IFS= read -r blob; do
      rm -f "$blob"
      removed=$((removed + 1))
    done < <(find "$dir/sha256" -maxdepth 1 -type f ! -name '.*' -mtime "+$days")
    printf 'artifact-cache: pruned %d blob(s) older than %d day(s)\n' "$removed" "$days"
    ;;

  status)
    [ -d "$dir" ] || { printf 'artifact-cache: %s does not exist\n' "$dir"; exit 0; }
    printf 'artifact-cache: %s (%s)\n' "$dir" "$(du -sh "$dir" | awk '{print $1}')"
    for blob in "$dir"/sha256/*; do
      [ -f "$blob" ] || continue
      printf '  sha256/%s  %s bytes\n' "${blob##*/}" "$(wc -c <"$blob" | tr -d ' ')"
    done
    for mirror in "$dir"/git/*/*.git; do
      [ -d "$mirror" ] || continue
      printf '  %s  %s\n' "${mirror#"$dir"/}" \
        "$(git -C "$mirror" for-each-ref --format='%(refname:short)@%(objectname:short)' refs/heads | tr '\n' ' ')"
    done
    ;;
esac
