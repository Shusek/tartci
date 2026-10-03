# Security hardening for tartci as the primary CI runner

A self-hosted runner executes whatever job GitHub assigns it. GitHub matches a
queued job to any registered runner whose labels it requests, so a runner is
only as trusted as the least trusted workflow that can name its labels. tartci
makes every job run in a disposable VM with a single-use JIT registration, which
limits what a job can do to *later* jobs. It does not decide *which* job a
runner receives. This guide covers both halves. Work through it before you
make tartci the main runner for a repository.

## Threat model

Treat every job as able to run arbitrary code as the guest's admin user. That
includes pull requests, a dependency's install script, or a compromised
action. Such a job must not be able to:

1. reach the runner in the first place, when it comes from someone you do not
   trust (a fork pull request, `pull_request_target`, another repository);
2. leave something behind that a later, more trusted job consumes (build
   caches, configure-check results, the golden);
3. reach the host, the LAN, the tailnet, or a sibling VM serving another job;
4. read host credentials: GitHub tokens, the operator's SSH agent, signing
   keychains, or another runner's JIT config.

## 1. Who can get a job onto the runner (GitHub settings)

- **Prefer private repositories.** Fork pull requests of a private repository
  only run if you enable that explicitly.
- **Public repositories:** set *Settings → Actions → General → Approval for
  running fork pull request workflows from contributors* to **Require approval
  for all external contributors**. tartci now refuses to register a runner for a
  public repository with any weaker policy. The exception is
  `TARTCI_ALLOW_PUBLIC_REPOSITORY_RUNNERS=1`, which you should set only after a
  documented review.
- **Never combine `pull_request_target` (or `workflow_run` acting on PR
  artifacts) with self-hosted labels.** Those events run with the base
  repository's secrets while checking out attacker-controlled code.
- Set the default `GITHUB_TOKEN` permissions to read-only, and keep deployment
  secrets in protected environments that PR workflows cannot use.
- **Organization runner groups.** Use them when your plan supports them. Use a
  dedicated group with *Selected repositories*, keep *Allow public
  repositories* off, and restrict it to *Selected workflows*. Pin trusted lanes
  to `@refs/heads/main`. Set `TARTCI_REQUIRE_WORKFLOW_RESTRICTION=1` so tartci
  refuses to mint unless GitHub enforces that allow-list. With a queue policy,
  every allowed workflow of the primary repository must also be one of the
  policy's `workflow_paths`.

The queue policy (`TARTCI_QUEUE_POLICY_FILE`, see `queue-policy.txt`) decides
when to *boot* a VM. It cannot stop GitHub from handing the runner another
label-matching job. On macOS, if GitHub assigns a run in the primary repository
that the policy refuses, the supervisor now stops the job and discards the VM.
That is containment after the fact, not prevention: the job may already have
run for a few seconds. The policy is refused outright with
`TARTCI_RUNNER_ASSIGNMENT_MODE=event-class-v2`, whose scanner does not read it.

## 2. Separate trust classes

Run **untrusted** work (pull requests, anything from outside contributors)
and **trusted** work (main, merge queue, release, anything with secrets) in
different lanes. Each lane has its own labels and its own cache root.

| Lane | Recommended settings |
| --- | --- |
| Untrusted (PR) | Prepared guest driver (no host shares, Softnet), **or** `TARTCI_HOST_CACHE_ACCESS=ro` + `TARTCI_TART_NETWORK=softnet` + its own `TARTCI_CI_CACHE` |
| Trusted (main / merge queue) | Its own `TARTCI_CI_CACHE`, `TARTCI_TART_NETWORK=softnet`; ideally a host that never runs PR code |
| Release, signing, deploy | GitHub-hosted runners or a dedicated host. Upstream keeps these hosted-only too (README "Shipyard profiles"). |

`TARTCI_HOST_CACHE_ACCESS=ro` mounts the ccache share read-only (and, on macOS,
the configure-check cache too) and runs the guest's ccache with
`CCACHE_READONLY`. On macOS, configure checks use a private copy that dies with
the VM. A PR job still gets warm hits from the trusted cache but cannot plant
objects a trusted build would link. It cannot be combined with
`TARTCI_CCACHE_WRITE_ISOLATION=1`. Write isolation only decides *when* a layer
is promoted (a green job), not *whose*, so it is not a trust boundary.

`TARTCI_TART_NETWORK=softnet` boots macOS and Linux guests with Tart Softnet,
which isolates each guest from the host, the LAN and its siblings. List any
destination a guest still needs in `TARTCI_TART_SOFTNET_ALLOW` as
comma-separated CIDRs, for example the host relay used by `GUEST_HTTP_PROXY`.
Softnet needs the `softnet` binary installed set-uid root (see Tart's docs).
Canary one lane before enabling it fleet-wide. Windows prepared guests already
use restricted QEMU networking; the shared Windows path does not, so keep it
off hosts that serve trusted work.

## 3. Credentials

- **GitHub:** use a GitHub App wrapper (`TARTCI_GH_CLI=ghapp`) or a
  fine-grained token for exactly the served repositories, instead of an
  operator's `gh auth login`. A fine-grained token needs Administration
  read/write, Actions read/write and Metadata read. Read the token from a 0600
  file in a small wrapper (see `scripts/tartci-m1-stackbench-jit-gh`). Never
  put a token in a plist.
- **Public repositories:** the access check also reads the fork-approval
  policy. That needs Administration read on the repository, which the JIT mint
  already requires for repository-scoped runners.
- **Guest SSH key:** use a dedicated key that opens nothing but CI guests
  (`TARTCI_VM_SSH_KEY`, `TARTCI_WIN_SSH_KEY`), not your everyday
  `~/.ssh/id_ed25519`. Providers now pass `IdentitiesOnly=yes`,
  `ForwardAgent=no` and `ForwardX11=no` whatever `~/.ssh/config` says.
- **JIT configs** reach every guest over stdin and never appear in host argv.
  The Linux provider used to embed it in the ssh command line.
- **Signing keychain** (Pulp-specific): the password now reaches
  `security -i` over stdin on every path. Keep `keychain.env` mode 0600, and
  keep signing off any host that runs PR code.

## 4. Goldens

- A golden must contain no secrets: no tokens, no `.runner`/`.credentials`
  files, no signing identities, no Tailscale identity or auth key.
  `tailscale up` belongs on hosts and persistent benches, never in a CI golden.
- SSH into a golden must be key-only. The Windows autounattend now generates a
  random admin password (stored 0600 as `admin-password` beside the media) and
  disables password SSH. The Linux bake installs
  `/etc/ssh/sshd_config.d/00-tartci-key-only.conf`. For hand-baked macOS
  goldens, add this before tagging:

  ```sh
  printf '%s\n' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' \
    | sudo tee /etc/ssh/sshd_config.d/000-tartci-key-only.conf
  sudo sshd -t
  ```

  Also turn off Screen Sharing / Remote Management unless a bench needs them.
  Change the base image's default `admin` password if the golden auto-logs in
  through a password you can rotate.
- **Pin runner binaries.** macOS verifies `TARTCI_RUNNER_SHA256`. Windows now
  refuses an unpinned download: set `TARTCI_WIN_RUNNER_SHA256` to the SHA-256
  from the actions/runner release notes, or bake the runner into the golden.

## 5. Host operations

- **Job deadlines:** macOS stops an assigned job after `TARTCI_JOB_TIMEOUT_SECS`
  (default 7200). Linux and the shared Windows path now do the same, defaulting
  to 21600 (GitHub's own default job timeout). Lower it to your longest real
  job.
- **Draining:** `tartci pool drain` lets an assigned job finish. A SIGTERM
  (`pool off`, `launchctl bootout`) defers an assigned prepared Windows job
  only until launchd's `ExitTimeOut` (20 s unless the plist sets it), then
  launchd kills the supervisor.
- **Updates:** keep fleet self-update disabled on a fork. It, the fleet
  installer and the support manifest trust only `danielraffel/tartci`. Pin
  hosts to a reviewed commit of this fork and update them explicitly.
- **Debug knobs:** never set `TARTCI_KEEP_FAILED=1` on a production lane. It
  keeps a failed guest running with its SSH forward open.

## Knob reference

| Variable | Default | Effect |
| --- | --- | --- |
| `TARTCI_ALLOW_PUBLIC_REPOSITORY_RUNNERS` | `0` | `1` admits a public repository whose fork approval is weaker than all external contributors |
| `TARTCI_REQUIRE_WORKFLOW_RESTRICTION` | `0` | `1` requires an org runner group restricted to selected workflows (and to the queue policy's paths) |
| `TARTCI_HOST_CACHE_ACCESS` | `rw` | `ro` mounts ccache/configure-check shares read-only, guest ccache read-only |
| `TARTCI_TART_NETWORK` | `shared` | `softnet` isolates macOS/Linux guests with Tart Softnet |
| `TARTCI_TART_SOFTNET_ALLOW` | empty | comma-separated CIDRs a Softnet guest may reach |
| `TARTCI_JOB_TIMEOUT_SECS` | `7200` macOS, `21600` Linux/Windows | host-side limit for an assigned job |
| `TARTCI_WIN_RUNNER_SHA256` | empty | required pin for a Windows runner download |
| `TARTCI_WIN_ADMIN_PASSWORD` | random | Windows golden admin password (`[A-Za-z0-9._-]`, at least 12 characters) |
