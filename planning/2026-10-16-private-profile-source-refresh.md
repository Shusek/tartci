# Private fleet profile source refresh

Status: implementation and verification repair merged; canary cleared; rollout gated

## Goal

Keep TartCI's checked-in profiles as the default for ordinary users while
allowing an operator to select a pinned, immutable profile source with clear
provenance. The public repository must not contain private fleet paths,
addresses, credentials, or host snapshots.

## What is in main

The source resolver and export validation landed in
https://github.com/danielraffel/tartci/pull/589. An external source is accepted
only when its export marker, profile set, advertised labels, repository
coordinates, and file bytes pass validation. Receipts record the source and
content digests used for installation. Without explicit source configuration,
TartCI continues to use its checked-in profiles.

## Canary status

An earlier canary was configured with a validated private export. The
transaction completed its admission, readiness, DHCP, and source checks, but
standalone verification reported a false `receipt_mismatch`: the apply process
selected the private source while the later verifier fell back to the public
support root. That configuration was cleared after the failed verification.
Current host receipts report the public source and no host is configured as an
external-source canary.

The verification repair is in
https://github.com/danielraffel/tartci/pull/605. It reconstructs the selected
source from the installed receipt, validates the marker and bytes, and restores
the process environment after resolution. It also keeps public and legacy
receipts on the immutable public support root even when private-source
variables are present. It must be installed on the canary and verified before
rollout resumes.

## Rollout gates

1. Install the current main generation, including the verification repair, on
   the selected canary.
2. Export the private authority at one immutable commit and configure only the
   canary to use it.
3. Run standalone verification and confirm pool readiness, source provenance,
   and rendered LaunchAgents.
4. Soak the canary and prove rollback to the checked-in source.
5. Obtain a separate go for each additional host and repeat the checks.

A host with a sealed launcher or an unsupported launcher layout must refuse the
external source before reading private data. A failed validation, missing
export, receipt mismatch, or failed rollback keeps that host on the public
default.

## Validation record

The focused source, source-sync, self-update, published-supply, lane, and
Python 3.9 compatibility tests pass on current main. The private authority's
semantic projection check also passes against fresh exact `origin/main`
checkouts of TartCI and Pulp. Public-path checks must remain clean. The
private fleet record and desired-state projection stay in a private authority
repository; they are not copied into this public tree.

## Scope

This plan covers profile-source selection, provenance, validation, canary
rollout, and rollback. Volume I/O, Tart inventory stalls, pool transitions, and
guest failures require separate changes and evidence.
