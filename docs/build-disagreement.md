# Cross-host build disagreement alarm

`tartci doctor build-disagreement` flags a gate host that fails to compile or
link code that another gate host builds green. That pattern is the visible
symptom of a poisoned shared compiler cache: the cache serves a wrong object
for a translation unit, and every build on that host fails the same way while
the source is fine.

It is read-only against GitHub, never changes scheduling, and is **off by
default**. It is one layer of several against shared-ccache poisoning; it
detects, it does not prevent or repair.

## Why it exists

On 2026-09-26 from about 08:23Z, every Pulp `macos` gate job on M3
(`studio-*` runners) failed to link `test/pulp-osc-render-wav` with an
undefined `write_scenario_wav`, because poisoned zero-include ccache manifests
served `wav_bridge.cpp.o` as `audio_doctor.cpp.o`. M5 built the same main green
throughout. It took about 17 hours to attribute, because each red, read on its
own, looked like "that PR is broken". Only the comparison across hosts says
"that host is broken".

## Rules

Inputs are completed jobs named `macos` in `Generous-Corp/pulp`'s
`build.yml`, listed through `actions/workflows/build.yml/runs` (the
`workflow_id` query parameter on `actions/runs` is ignored by GitHub) and
`actions/runs/<id>/jobs?filter=all`, so re-run attempts count. Runner names map
to hosts: `studio-*` and `m3-*` are M3, `m5-*` is M5, `m1-*` is M1.
GitHub-hosted jobs are out of scope. A job's outcome is its `Build` step: a
job whose Build step succeeded and whose tests failed still counts as a green
build.

- **same_build** — the same built identity has a green Build on host A and a
  Build-step failure on host B, B never built that identity green, and B's log
  carries a compile/link signature. The identity is the head SHA (also matched
  by tree) for events that build their head commit (`push`, `merge_group`,
  `workflow_dispatch`), and the (head, base) pair for `pull_request`, whose
  built commit is the synthetic merge ref.
- **streak** — host B's last K (default 3) Build outcomes are all failures
  with the same error fingerprint across at least two distinct identities,
  while another host completed a green Build during the streak.

Compile/link signatures: `Undefined symbols for architecture`, `undefined
reference to`, `ld: symbol(s) not found`, `ld: <error>` (never `ld: warning`,
which every green build prints), `linker command failed`, and
`<file>.<c|cpp|mm|...>:<line>: error:`. A log that also shows a killed
compiler, a full disk or OOM is a resource failure and does not count. The
fingerprint is the set of undefined symbols or error lines with paths and line
numbers removed.

A finding names the host, commit, failing job and its URL, the green
counterpart job, the error fingerprint and the remedy. A log that cannot be
read yields `unknown` (`disagreement_log_unreadable`), never `problem`.

## Detection floor

- An identity built on only one host cannot be compared. Merge-queue commits
  are usually built once, and pushes to main reuse the merge-queue receipt, so
  **same_build** mostly sees re-runs and repeated PR builds. **streak** is the
  rule that catches a host that fails everything.
- A streak shorter than K stays silent.
- If only one host is serving the gate, a poisoned host and a broken main are
  indistinguishable. The check says so rather than guessing.
- Log reads are capped (`max_log_fetches`, default 12) and API calls are capped
  (`max_api_calls`, default 200). A capped read reports `unknown`.

## Enabling

Per invocation:

```sh
tartci doctor build-disagreement --enable            # text
tartci doctor build-disagreement --enable --json     # exit 1 on a problem finding
```

Per host, in the fleet profile (no checked-in profile enables it):

```toml
[build_disagreement]
enabled = true
# Optional tuning; these are the defaults.
repo = "Generous-Corp/pulp"
workflow = "build.yml"
hours = 6
streak = 3
max_api_calls = 200
max_log_fetches = 12
```

Then `tartci doctor build-disagreement --profile <name>`.

GitHub reads go through `TARTCI_GH_CLI` (default `gh`; `ghapp` works, and must
run from inside a checkout of the repo). Job logs need a CLI that accepts
`--allow-escape-sequences`, since current `gh` refuses to print the colour
codes Actions logs carry; set `TARTCI_GH_LOG_CLI` (or `--log-cli`) to a plain
`gh` binary if the main CLI is a wrapper that rejects that flag.

`--from-jobs <file> --logs-dir <dir> --now <time>` replays a recorded job list
offline; `--dump-jobs <file>` records one.

## Remedy

On the flagged host run `tartci ccache reset`: it quarantines zero-include
manifests, `--reset` also moves the whole shared cache aside, `--plan` shows
what it would do, and it refuses while a VM runs or holds a lease unless
`--force`. On a host whose installed tartci predates that command, drain its
gate lanes, move its shared ccache directory aside and resume. Then re-run the failing job named in the finding and confirm
it goes green on that host.
