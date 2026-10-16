# Private fleet profile source refresh

Status: M1 canary installed; standalone verification fix prepared; rollout remains gated

## Goal

Keep TartCI's checked-in profiles as the default for ordinary users while
allowing an operator to select a pinned, immutable fleet profile source with
clear provenance. The public repository must not contain private fleet paths,
addresses, credentials, or host snapshots.

## Current authority

The private fleet record is maintained outside this repository. It is a
snapshot and rebuild record, not a directly consumable TartCI profile source.
Its collector redacts sensitive values and stores profiles below per-machine
paths, while TartCI's external-source contract requires a validated export with
`profiles/*-macos-fleet.toml`, `fleet/advertised-labels.json`, and an export
marker. A redacted snapshot must therefore never be selected as a live source.

The private authority was compared with the current TartCI profile contract.
It does not yet contain a complete validated export, so no private snapshot is
being copied into this repository or activated on a host as part of this
change.

## Implementation

1. Strengthen source resolution so an explicit source is rejected before use
   when its files are not regular files, its profile metadata does not name the
   exact exported profile set, or a profile is not parseable TOML.
2. Keep the default source path unchanged when no external source is selected.
3. Include source tree, profile, and published-supply digests in selected-source
   provenance so receipts can identify the exact bytes used.
4. Add focused tests for malformed profiles, metadata/profile-set drift,
   symlinked files, and provenance digests.
5. Keep the private fleet record as the backup and desired-state authority. A
   future generated export may be used only after it is produced from a live,
   unredacted host configuration and passes the same validation.

## Validation gates

- Run the focused source and self-update tests in a fresh worktree.
- Run the related macOS lane/source tests and `./scripts/lint.sh` on M1.
- Ask Ultra for adversarial review of the authority decision and changes.
- Confirm the public-path audit finds no private addresses, paths, tokens, or
  redacted snapshot data.
- Do not configure or roll out an external source until a real unredacted
  export exists and its receipt, profile validation, canary, soak, and rollback
  gates pass.

## Rollout status

Only the M1 canary has been changed. M1 has the pinned private source and its
pool was returned to service after the canary checks. M3, M5, and M5s remain on
the public source; widening and rollback completion remain gated on clean
standalone verification and a soak period.

## Scope boundary

This change improves source integrity and receipt evidence. It does not repair
volume I/O, Tart inventory stalls, pool transitions, or guest failures. Those
need separate, narrowly scoped changes with their own canary and rollback
evidence.

## Progress update

The resolver now rejects symlinked source files, malformed profile TOML,
profile metadata that names a different file set, malformed repository
coordinates, and non-UTF-8 marker or supply files. Selected-source receipts now
include the profile, published-supply, and complete-tree digests. The focused
source, source-sync, self-update, published-supply, lane, and installer tests
pass. The exact published head was checked out in an isolated M1 worktree;
M1 lint and the source, source-sync, and self-update suites passed there.

The branch is published for review at
`feat/private-profile-source-refresh-20261016`. It has not been merged or
installed on a fleet host.

The private authority now has a complete immutable export in the private
authority repository. M1 was selected as the least capacity-critical canary
and installed the pinned source. Pool admission, readiness, DHCP, and the
source/profile/supply checks passed during the canary transaction.

The first independent post-canary verification exposed a real bug in the
verification path: the apply process exported the selected source through
environment variables, but standalone `self-update --verify` did not inherit
them. It therefore compared the receipt with the public support root and
reported a false `receipt_mismatch`. The fix reconstructs the selected source
from the installed receipt, validates its marker and bytes, and restores the
process environment after resolution. Focused source and lane tests pass after
the fix. No additional host will be configured until the fix is reviewed,
installed on M1, and the standalone verification passes.

The fix is published for review at
https://github.com/danielraffel/tartci/pull/602, head
https://github.com/danielraffel/tartci/commit/7dfbebf114f2774edce01a0833304470f6b1dd84.

The private desired-state projection was reconciled against exact current
TartCI and Pulp `origin/main` checkouts on 2026-10-16. Its checker passed the
reconciled projection and rejects missing, empty, malformed, unknown, duplicate,
and stale host tier data. The projection is kept in the private authority
repository and is not part of the public TartCI source tree.

The first Python 3.9 run exposed a compatibility gap: stock Python 3.9 has no
`tomllib`, so valid configured sources were rejected. Export now performs full
TOML validation under the pinned 3.11+ interpreter before writing its immutable
marker; Python 3.9 verifies the marker and resolves the already-validated
bytes. The source, sync, self-update, and system-Python coverage tests pass
locally after this fix.

The focused suites were rerun from a clean current-main worktree on
2026-10-16: `test_fleet_source.py` passed 23 tests,
`test_fleet_source_sync.py` passed 15 tests, and
`test_fleet_self_update.py` passed 314 tests. The private projection checker
tests pass 6 tests. Ultra approved the private projection at its current
review head. External-source selection remains disabled while the complete
export is still missing.

## Stop conditions

Stop and retain the public default if the private authority remains redacted,
if its generated export cannot be authenticated, if profile validation or
provenance disagrees, if M1 validation fails, or if the canary cannot prove
pool readiness and rollback.
