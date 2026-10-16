# Private fleet profile source refresh

Status: implementation complete; external-source rollout intentionally gated

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

The private record was compared with TartCI `origin/main` at
`396772a3686cf071145fea42f851cf328cb76b07`. The comparison found:

- the private snapshot has no top-level published supply document;
- the private profile files contain redacted values and cannot be parsed as
  runnable TOML without restoration from the host;
- the m1 snapshot is behind current public profile routing and lacks the newer
  toolchain declarations;
- the m3 and m5 profiles are structurally close, with private path and secret
  values redacted;
- the m5s snapshot is structurally close but has the sealed launcher policy and
  host-specific values that must not be copied into public TartCI.

No private snapshot is being copied into the public repository or activated on
a host as part of this change.

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

No fleet mutation has been performed. Current installed hosts report the
ordinary checked-in profile path and no configured external source. The canary
and rollback steps remain gated on a validated private export.

## Fleet evidence review

Read-only checks on m1, m5, and m5s found no OS reboot corresponding to the
reported m5s disruption. m5s has been up since 2026-10-11 21:00 UTC. The
recorded recovery failures were pool and launcher recovery failures caused by
slow I/O on the external VM volume, followed by a successful watchdog recovery
after 18 attempts. m5 has older recovery records for network relay readiness
and a separate external VM count limit. These are real TartCI reliability
issues, but they are outside the profile-source change and must not be reported
as fixed by it.

The profile-source change improves source integrity and receipt evidence. It
does not repair volume I/O, Tart inventory stalls, pool transitions, or Windows
guest failures. Those need separate, narrowly scoped changes with their own
canary and rollback evidence.

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

The current private repository tree was rechecked after implementation. It
still contains only `machines/<host>/tartci/config/` snapshots and no
`profiles/` export or `fleet/advertised-labels.json`. The live m3 profile could
not be collected because its configured SSH key is rejected. A partial export
from the reachable hosts would omit capacity and routing authority, so it is
not being created or rolled out.

The first GitHub Python 3.9 run exposed a compatibility gap in the initial
validator: stock Python 3.9 has no `tomllib`, so valid configured sources were
rejected. The resolver now uses a conservative syntax check on 3.9 and keeps
full TOML parsing on 3.11+. The affected 3.9 source, peer, tailnet, and serving
tests pass locally after the fix; the replacement CI run is pending.

## Stop conditions

Stop and retain the public default if the private authority remains redacted,
if its generated export cannot be authenticated, if profile validation or
provenance disagrees, if M1 validation fails, or if the canary cannot prove
pool readiness and rollback.
