# Suvio extensions in Shusek/tartci

Upstream: [danielraffel/tartci](https://github.com/danielraffel/tartci).
The initial fork is based on `d7dba4d4e1692635c902d37db85dc47673ca0179`
(PR #355), with our changes carried as separate commits.

## Extensions

- Prepared guest drivers for macOS/Tart and Windows/QEMU, using the provider's
  clone, lease, JIT, monitoring, drain and disposal lifecycle. This preserves
  preinstalled toolchains, downloaded iOS runtimes and warmed guest caches.
  See [prepared guest drivers](prepared-guests.txt).
- Explicit lease CPU/RAM budgets and a non-gate CPU clamp, allowing resources
  to be reserved for a controller outside the TartCI lease store.
- Queue admission policies based on exact repository, event, workflow path,
  push branch and same-repository PR evidence. Cached observations are
  revalidated against the policy.
- Explicit organization/repository registration scope, independent of runner
  group ID, plus observation of every declared repository in a shared group.
  Foreign assignments are observed without cancelling or rerunning a job
  through the primary repository's API. See [queue policy](queue-policy.txt).

- Security hardening for running tartci as the primary CI runner: a
  public-repository fork-approval gate and optional workflow allow-list at every
  JIT mint, enforcement of the queue policy at assignment time, no runner or
  keychain credentials in host argv, a pinned Windows runner archive,
  symlink-safe ccache promotion, opt-in read-only host caches and Tart Softnet,
  host job deadlines on every provider, and key-only SSH in baked goldens.
  See [security hardening](security-hardening.md).

The driver mode is optional; existing provider bootstrap remains available.
Queue admission is separate from GitHub's eventual job assignment. A live lane
still needs a real GitHub job, drain and restart-recovery check before activation.

## Upstream changes reviewed for this update

The 2026-10-07 synchronization includes upstream through
`09e70b721dd7f5f9fcd76d021e04140d0acec06c` (PR #425): 207 commits and 169 changed
files since the previous `74153a1` snapshot. It adds bounded launchd recovery,
VM deletion evidence, DHCP diagnostics and a boot breaker, peer boot claims,
home-volume pressure reporting, cache/worktree maintenance, Python 3.9 test
coverage, and hardened disposable Windows/QEMU validation. Windows Skia and
Proxmox proof support target Pulp; they do not change Suvio's GraalVM profile.

The merge preserves the fork's prepared-driver lifecycle, explicit resource
budgets, queue and runner-group admission, stdin credential transport, cache
isolation and Softnet configuration. Three textual conflicts were resolved by
keeping those changes alongside the new diagnostic and interpreter helpers.
Prepared drivers bypass the native DHCP breaker and direct SSH spawn diagnostics:
their Softnet readiness and diagnostic endpoint belong to the protected driver,
and they do not publish native DHCP probe receipts. Regression tests prove that
both local and JIT jobs still complete and dispose their VM when a native DHCP
probe is occupied, without changing its state or issuing direct SSH commands.

This synchronization changes the fork source. The installed Suvio service
remains pinned to `ac966b479e926e0cdb5133c52be884c19a2ab7db`; deployment of a newer
generation requires a separate private-host canary, job drain and teardown check.

### Initial review

From PR #337 (`d254cf8`) to PR #355 (`d7dba4d`), upstream added 55 commits
(including merges), changing 41 files. The main changes are:

- Fleet updates use a consistent queue clock and ticket order, respect
  Shipyard's writer lease, and treat an idle-wait capacity refusal as a refusal.
- launchd runner and support agents use the installed generation instead of
  stale checkout paths or copied scripts. The watchdog refreshes loaded
  attestation and queue-saturation agents while retaining host settings.
- Reclaim bounds directory listings, reports timeouts/refusals, skips app
  containers and Library, rejects whole-volume scan roots, and uses Standard QoS.
- The network relay reports a held port and throttles retries; its template
  follows the installed generation. `queue-tick --help` only prints usage.
- Capacity-floor checks can count a peer that demonstrably mints runners on
  demand. Host attestation evaluates registrations against the relevant repo.

None of those 41 files overlaps the original 17-file extension patch.

## Maintenance and validation

Keep `origin` pointing at this fork and `upstream` at danielraffel/tartci.
Merge reviewed upstream changes into `main`, retaining separate extension
commits. Pin installations to a tested commit and update them explicitly.
Upstream's fleet self-update currently binds danielraffel/tartci; leave that
automation disabled for this fork until fork-aware update support is added.

Run the repository's existing CI checks:

```sh
./scripts/lint.sh
python3 -m unittest discover -s scripts -p 'test_*.py' -v
for test_script in tests/test_*.sh; do bash "$test_script" || exit; done
```

The same workflow can be started manually with
`gh workflow run ci.yml --repo Shusek/tartci --ref main`. Its jobs use
GitHub-hosted Ubuntu runners and do not register or change a VM runner.

The prepared-driver tests exercise local success/failure, JIT transport,
assigned-job drain and cleanup with fake providers. Actual platform builds
should also run against the host's private prepared goldens before deployment.

Host-specific adapters, configuration, App/SSH keys, tokens, VM images and
build caches stay outside this repository. Publishing or updating this fork
does not install services or switch a GitHub runner.
