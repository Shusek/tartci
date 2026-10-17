# Private fleet profile source refresh

Status: implementation is on main; the canary and soak passed; the rollout fix is awaiting signed review.

## Goal

Keep TartCI's checked-in profiles as the default for ordinary users while
allowing an operator to select a pinned, immutable fleet profile source with
clear provenance. The public repository must not contain private fleet paths,
addresses, credentials, or host snapshots.

## Current authority

The private fleet record is maintained outside this repository. It is a
snapshot and rebuild record, not a directly consumable TartCI profile source.
Its collector may redact sensitive values and store profiles below private
machine paths, while TartCI's external-source contract requires a validated
export with `profiles/*-macos-fleet.toml`, `fleet/advertised-labels.json`, and
an export marker. A redacted snapshot must never be selected as a live source.

The private authority was compared with the current TartCI profile contract.
The validated export contains the required profile set and published supply
document. The export is selected by commit and carries a generated marker; no
private paths or host data are added to this repository.

## Implementation

1. Reject an explicit source before use when its files are not regular files,
   its profile metadata does not name the exact exported profile set, or a
   profile is not parseable TOML.
2. Keep the default source path unchanged when no external source is selected.
3. Include source tree, profile, and published-supply digests in selected-source
   provenance so receipts identify the exact bytes used.
4. Test malformed profiles, metadata/profile-set drift, symlinked files, and
   provenance digests.
5. Keep the private fleet record as the backup and desired-state authority. A
   generated export may be used only after it is produced from a live,
   unredacted configuration and passes the same validation.

## Validation gates

- Run the focused source and self-update tests in a fresh worktree.
- Run the related macOS lane/source tests and `./scripts/lint.sh` on the
  canary host.
- Obtain an independent adversarial review of the authority decision and changes.
- Confirm the public-path audit finds no private addresses, paths, tokens, or
  redacted snapshot data.
- Do not configure or roll out an external source until a real unredacted
  export exists and its receipt, profile validation, canary, soak, and rollback
  gates pass.

## Rollout status

The private projection reconciles cleanly against fresh detached checkouts of
the current TartCI and Pulp sources. An earlier canary installed the validated
export, recorded its repository, commit, profile digest, published-supply
digest, and complete-tree digest, and passed verification with every expected
supervisor serving. That canary was later cleared after standalone verification
reported a false receipt mismatch. Current receipts use the public source, no
external-source canary is active, and the fleet is ready.

The earlier canary passed the required 30-minute soak. Wider rollout remains
blocked while the source-move sequencing fix is reviewed and until each host
receives its own readiness, live canary assignment and teardown, and rollback
proof. Sealed launcher hosts remain on the public source until their launcher
profiles support this source selection.

## Scope boundary

This change improves source integrity and receipt evidence. It does not repair
volume I/O, Tart inventory stalls, pool transitions, or guest failures. Those
need separate, narrowly scoped changes with their own canary and rollback
evidence.

## Progress update

The resolver rejects symlinked source files, malformed profile TOML, profile
metadata that names a different file set, malformed repository coordinates,
and non-UTF-8 marker or supply files. Selected-source receipts include the
profile, published-supply, and complete-tree digests. The focused source,
source-sync, self-update, published-supply, lane, and installer tests pass.

The source resolver and provenance changes are on TartCI `main`. The private
authority remains the desired-state and backup authority; the public checkout
continues to be the default when no source is configured.

The first Python 3.9 run exposed a compatibility gap: stock Python 3.9 has no
`tomllib`, so valid configured sources were rejected. Export now performs full
TOML validation under the pinned interpreter before writing its immutable
marker; Python 3.9 verifies the marker and resolves the already-validated
bytes. The source, sync, self-update, and system-Python coverage tests pass
locally after this fix.

## Stop conditions

Stop and retain the public default if the private authority becomes redacted,
if its generated export cannot be authenticated, if profile validation or
provenance disagrees, if canary validation fails, or if the canary cannot prove
pool readiness and rollback. Do not widen the rollout from a failed or
unverified canary.

## 2026-10-17 canary follow-up

During an earlier canary follow-up, an agent session re-armed the canary
without authorization. It exported the selected source, but the canary stopped
before installing it; the operator reverted the configuration, and the host
remained on its previous verified generation and serving normally. The
self-update rate limiter had treated the unchanged TartCI generation as the
whole attempt identity, so a recent public-profile install blocked the source
move for six hours.

The fix is in [PR 606](https://github.com/danielraffel/tartci/pull/606). It
keeps the rate limit by generation and source class, preserving the existing
same-source backoff while allowing a deliberate public-to-private move to run
its own guarded canary. Self-update is currently running with no external
source configured, so the public source remains the default. Resuming the
private-source rollout requires a separate per-host go after the fix is reviewed
and landed.

## 2026-10-17 update

The first wider rollout check found a sequencing bug. A host had current TartCI
code while the next TartCI commit was still soaking. A private-source move was
then treated as a no-op, so the new source was not installed. PR
[604](https://github.com/danielraffel/tartci/pull/604) fixes that case and adds
regression coverage.

The Python floor, self-update, and lint checks pass on the current candidate.
The verification repair is merged in [PR 605](https://github.com/danielraffel/tartci/pull/605),
and the cleared canary currently reports a ready pool with no receipt mismatch.
The wider rollout remains stopped pending signed review of [PR
606](https://github.com/danielraffel/tartci/pull/606) and a separate go for each
host. Sealed launcher hosts remain on the public source until they support this
selection.

The authority check was rerun on fresh detached checkouts on 2026-10-17 and
passed with `local macOS fleet projection: OK`.
