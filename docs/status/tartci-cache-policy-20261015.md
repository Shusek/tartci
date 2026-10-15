# TartCI cache policy status ledger

- Scope: TartCI/Shipyard cache-policy improvements for repeat Windows/Linux builds.
- Base SHA: `c90edfb0795396fcc6c58846189cce3de4ea118c` (fresh worktree from `origin/main`).
- Implementation SHA: updated on the PR head after review repair; see the exact PR head in the completion report.
- PR: https://github.com/danielraffel/tartci/pull/547
- Ownership limits: no live UTM or Proxmox builds, VM images, runners, macOS capacity, Windows runner registration, or TartCI PR 546.
- Evidence reviewed: Linux Tart runner already propagates the granted lease to `CMAKE_BUILD_PARALLEL_LEVEL`; Windows QEMU runner sized the VM from the lease but did not propagate the grant to the Actions job. Active build measurements supplied for comparison: ARM UTM governed at 1 core; Proxmox x64 SDK on `B:` with FetchContent junction governed at 4 cores. The active Pulp Windows workflow explicitly uses build `--parallel 4` and tests `-j8`, so those steps remain unchanged.
- Change: Windows JIT listener now exports the lease-granted `effective_win_cpus` as `CMAKE_BUILD_PARALLEL_LEVEL` and emits a diagnostic. Explicit workflow `-j` remains caller-owned.
- Behavior test: `--print-build-env-script 4` emits the generated PowerShell consumed by the runner; value `8` is also parameterized and zero is rejected. Explicit workflow `--parallel`/`-j` overrides remain outside this change.
- Tests: `bash -n providers/qemu-windows/runner.sh`; targeted Windows propagation + Linux lifecycle (14 tests); full VM lease helper, admission-clean, and cache-routing suites (102 tests), all PASS.
- Limits: this changes the Windows job environment only; it does not add sccache, FetchContent persistence, or prove physical Windows cache reuse. A separate cache mount needs a Windows guest transport and exact cache ownership proof.
