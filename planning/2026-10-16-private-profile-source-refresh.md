# Private fleet profile source refresh

Status: implementation is on main; M1 canary and soak passed; the M5 rollout fix is awaiting signed review

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
Its current `main` at `55a2617d4705f1becf48ee41b96cc26697607db0` contains the
four fleet profiles and published supply document required by the source
contract. The export is selected by commit and carries a generated marker; no
private paths or host data were added to this public repository.

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

The private projection reconciles cleanly against exact current source heads:
Pulp `8d2463527a1dfd51635ef3d4189e00e50c2ed82e` and TartCI
`5df49763eeef6d1aa9657b1c8a5a12cd21275749`.

M1 is the canary. It installed TartCI `eef056df977a9bf1e74a66266ec15f950a9948fd`
with private source `danielraffel/pulp-planning@65c2aec0a6f6d17ce2ab671bff09fb55c4692142`.
The install receipt records the repository, commit, profile digest,
published-supply digest, and complete-tree digest. The update drained and
restored the pool with all required labels covered, then passed verification
with five of five supervisors serving. A persistent `[fleet_source]` setting
is installed on M1 so scheduled updates keep the same source selection.

M1 passed the required 30-minute soak. Wider rollout remains blocked until the
M5 sequencing fix is reviewed and merged, then receives its own readiness,
live canary assignment and teardown, and rollback proof.

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

The source resolver and provenance changes are on TartCI `main`. The private
authority remains the desired-state and backup authority; the public checkout
continues to be the default when no source is configured.

The first Python 3.9 run exposed a compatibility gap: stock Python 3.9 has no
`tomllib`, so valid configured sources were rejected. Export now performs full
TOML validation under the pinned 3.11+ interpreter before writing its immutable
marker; Python 3.9 verifies the marker and resolves the already-validated
bytes. The source, sync, self-update, and system-Python coverage tests pass
locally after this fix.

## Stop conditions

Stop and retain the public default if the private authority becomes redacted,
if its generated export cannot be authenticated, if profile validation or
provenance disagrees, if M1 validation fails, or if the canary cannot prove
pool readiness and rollback. Do not widen the rollout from a failed or
unverified canary.

## 2026-10-17 update

The first M5 rollout check found a real sequencing bug. M5 had the current
TartCI code while the next TartCI commit was still soaking. A private-source
move was then treated as a no-op, so the new source was not installed. PR
[604](https://github.com/danielraffel/tartci/pull/604) fixes that case and has
the regression test. Its exact head is
[5d11a384](https://github.com/danielraffel/tartci/commit/5d11a3841f2edb994c3f2dc906d29fe462affc97).

The Python 3.9, Python floor, and lint checks pass. The required signed
`infra-review2` approval is still pending, so M5 remains on the public source.
Its private-source setting is staged but has not been activated. M1 remains
healthy on the private source. M3 and m5studio remain on the public source
until their sealed launcher profiles support this source selection.

The current private planning `main` is
`7f4dabcbb0d97b1e05f4fe4a00e9ed613b567ced`. Its profile and advertised-label
files have the same SHA-256 values as the installed M1 source, so the newer
private commits contain no profile drift.
