#!/usr/bin/env python3
"""Per-job ccache write layers: verdicts, promotion rules, sweep, config."""

from __future__ import annotations

import errno
import testing_support  # noqa: E402
testing_support.skip_module_without_tomllib()
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ccache_layer as cl  # noqa: E402
import macos_fleet_lanes as fleet  # noqa: E402

LIB = ROOT / "providers" / "tart-macos" / "ccache-layer.lib.sh"
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
PROMOTER = ROOT / "scripts" / "ccache_layer.py"
SUCCESS_LOG = "2026-09-27 01:00:00Z: Job macos completed with result: Succeeded\n"


def entry(kind: int, payload: bytes = b"payload") -> bytes:
    return b"\xcc\xac\x01" + bytes([kind]) + b"\x00" * 16 + payload


def write_entry(remote: Path, key: str, data: bytes, mtime: float | None = None) -> Path:
    path = remote / key[:2] / key[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def never_present(_vm: str) -> bool:
    return False


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccache-layer-"))
        self.layout = cl.Layout(self.tmp / "ccache" / "tartci-layers-v1", self.tmp / "state")
        self.log = self.tmp / "runner.log"
        self.log.write_text(SUCCESS_LOG)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def attach(self, vm: str, owner: int | None = None) -> Path:
        return cl.attach(self.layout, vm, os.getpid() if owner is None else owner)

    def settle(self, vm: str, rc: int = 0, **kwargs: object) -> tuple[str, str, Path | None]:
        return cl.settle(self.layout, vm, rc, str(kwargs.get("capture", "")),
                         str(kwargs.get("receipt", "")), str(kwargs.get("quarantine", "none")),
                         kwargs.get("log", self.log))  # type: ignore[arg-type]

    def shared_keys(self) -> set[str]:
        return {f"{p.parent.name}{p.name}" for p in self.layout.shared.glob("*/*")
                if not p.name.startswith(".")}

    def audit_events(self) -> list[dict]:
        if not self.layout.audit.exists():
            return []
        return [json.loads(line) for line in self.layout.audit.read_text().splitlines()]


class VerdictTests(unittest.TestCase):
    def test_green_needs_clean_exit_and_success(self) -> None:
        self.assertEqual(cl.verdict(0, "", "", "none", "Succeeded")[0], "green")
        self.assertEqual(cl.verdict(0, "", "", "none", "SucceededWithIssues")[0], "green")
        self.assertEqual(
            cl.verdict(0, "terminal", '{"kind":"terminal","conclusion":"success"}', "none",
                       "Succeeded")[0], "green")

    def test_everything_short_of_proof_is_red(self) -> None:
        cases = {
            "timeout": (124, "", "", "none", "Succeeded"),
            "retarget": (125, "", "", "none", None),
            "killed": (143, "", "", "none", None),
            "failed": (0, "", "", "none", "Failed"),
            "cancelled": (0, "", "", "none", "Canceled"),
            "no_line": (0, "", "", "none", None),
            "quarantine": (0, "active", "", "listener_exited_workflow_active", "Succeeded"),
            "api_failure": (0, "terminal", '{"conclusion":"failure"}', "none", "Succeeded"),
            "api_cancel": (0, "terminal", '{"conclusion":"cancelled"}', "none", "Succeeded"),
            "bad_receipt": (0, "terminal", "{not json", "none", "Succeeded"),
        }
        for name, args in cases.items():
            with self.subTest(name):
                self.assertEqual(cl.verdict(*args)[0], "red")

    def test_runner_log_result_takes_the_last_completion_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "log"
            log.write_text("x: Job a completed with result: Failed\n" + SUCCESS_LOG)
            self.assertEqual(cl.runner_log_result(log), "Succeeded")
            self.assertIsNone(cl.runner_log_result(Path(td) / "missing"))


class PromotionTests(Base):
    def test_green_promotes_into_shared_and_removes_the_layer(self) -> None:
        layer = self.attach("vm-a")
        write_entry(layer / "remote", "ab" + "1" * 38, entry(0))
        write_entry(layer / "remote", "cd" + "2" * 38, entry(1))
        decision, _, target = self.settle("vm-a")
        self.assertEqual(decision, "green")
        self.assertEqual(target, self.layout.green / "vm-a")
        self.assertEqual(cl.process(self.layout, "vm-a", never_present, poll=0.01), "promoted")
        self.assertEqual(self.shared_keys(), {"ab" + "1" * 38, "cd" + "2" * 38})
        self.assertFalse((self.layout.green / "vm-a").exists())
        promote = [e for e in self.audit_events() if e["event"] == "promote"]
        self.assertEqual(promote[0]["promoted"], 2)
        self.assertEqual(len(list(self.layout.promotions.glob("*-vm-a.keys"))), 1)

    def test_red_cancel_and_timeout_discard_without_touching_shared(self) -> None:
        for vm, rc, log_text in (("vm-red", 0, "x: Job m completed with result: Failed\n"),
                                 ("vm-cancel", 0, "x: Job m completed with result: Canceled\n"),
                                 ("vm-timeout", 124, SUCCESS_LOG)):
            with self.subTest(vm):
                layer = self.attach(vm)
                write_entry(layer / "remote", "ef" + "3" * 38, entry(0))
                log = self.tmp / f"{vm}.log"
                log.write_text(log_text)
                decision, _, target = self.settle(vm, rc, log=log)
                self.assertEqual(decision, "red")
                self.assertEqual(target.parent, self.layout.discard)
                self.assertEqual(cl.process(self.layout, target.name, never_present), "discarded")
                self.assertFalse(target.exists())
        self.assertEqual(self.shared_keys(), set())
        self.assertFalse([e for e in self.audit_events() if e["event"] == "promote"])

    def test_existing_result_is_never_overwritten(self) -> None:
        key = "ab" + "4" * 38
        original = write_entry(self.layout.shared, key, entry(0, b"original"), mtime=1.0)
        layer = self.attach("vm-b")
        write_entry(layer / "remote", key, entry(0, b"newer-but-a-result"))
        self.settle("vm-b")
        cl.process(self.layout, "vm-b", never_present)
        self.assertEqual(original.read_bytes(), entry(0, b"original"))

    def test_manifest_replaced_only_when_strictly_newer(self) -> None:
        older_key, newer_key = "aa" + "5" * 38, "bb" + "6" * 38
        write_entry(self.layout.shared, older_key, entry(1, b"shared-old"), mtime=1000.0)
        write_entry(self.layout.shared, newer_key, entry(1, b"shared-new"), mtime=5000.0)
        layer = self.attach("vm-c")
        write_entry(layer / "remote", older_key, entry(1, b"job"), mtime=3000.0)
        write_entry(layer / "remote", newer_key, entry(1, b"job"), mtime=3000.0)
        self.settle("vm-c")
        cl.process(self.layout, "vm-c", never_present)
        self.assertEqual((self.layout.shared / "aa" / older_key[2:]).read_bytes(), entry(1, b"job"))
        self.assertEqual((self.layout.shared / "bb" / newer_key[2:]).read_bytes(),
                         entry(1, b"shared-new"))

    def test_malformed_symlinked_and_non_entry_files_are_rejected(self) -> None:
        outside = self.tmp / "outside-secret"
        outside.write_bytes(entry(0))
        layer = self.attach("vm-d")
        remote = layer / "remote"
        write_entry(remote, "ab" + "7" * 38, b"not a ccache entry at all")
        write_entry(remote, "ab" + "8" * 38 + ".tmp", entry(0))
        (remote / "zz").mkdir()
        (remote / "zz" / ("9" * 38)).symlink_to(outside)
        (remote / "yy").symlink_to(self.tmp)
        write_entry(remote, "QZ" + "1" * 38, entry(0))  # upper case is not a ccache key
        self.settle("vm-d")
        cl.process(self.layout, "vm-d", never_present)
        self.assertEqual(self.shared_keys(), set())
        promote = [e for e in self.audit_events() if e["event"] == "promote"][0]
        self.assertEqual(promote["rejected"], 5)

    def test_concurrent_promotions_are_safe(self) -> None:
        keys = [f"{i:02x}" + "c" * 38 for i in range(40)]
        names = [f"vm-par-{n}" for n in range(8)]
        for n, vm in enumerate(names):
            layer = self.attach(vm)
            for key in keys:
                write_entry(layer / "remote", key, entry(0, f"from-{n}".encode()))
            write_entry(layer / "remote", "ff" + "d" * 38, entry(1, f"m{n}".encode()),
                        mtime=1000.0 + n)
            self.settle(vm)
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            outcomes = list(pool.map(
                lambda vm: cl.process(self.layout, vm, never_present), names))
        self.assertEqual(outcomes, ["promoted"] * len(names))
        self.assertEqual(self.shared_keys(), set(keys) | {"ff" + "d" * 38})
        for key in keys:
            self.assertRegex((self.layout.shared / key[:2] / key[2:]).read_bytes()[20:].decode(),
                             r"^from-[0-7]$")
        self.assertEqual((self.layout.shared / "ff" / ("d" * 38)).read_bytes(), entry(1, b"m7"))
        self.assertFalse(list(self.layout.shared.glob(f"*/{cl.TMP_PREFIX}*")))
        promoted = sum(e["promoted"] for e in self.audit_events() if e["event"] == "promote")
        self.assertEqual(promoted, len(keys) + 1)

    def test_concurrent_cli_processes_on_one_layer_run_it_once(self) -> None:
        layer = self.attach("vm-cli")
        write_entry(layer / "remote", "ab" + "e" * 38, entry(0))
        self.settle("vm-cli")
        fake_tart = self.tmp / "tart"
        fake_tart.write_text("#!/bin/sh\nsleep 0.3; printf '[]'\n")
        fake_tart.chmod(0o755)
        cmd = [sys.executable, str(PROMOTER), "--root", str(self.layout.root),
               "--state", str(self.layout.state), "process", "--layer", "vm-cli",
               "--tart", str(fake_tart), "--poll", "0.05"]
        procs = [subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True) for _ in range(4)]
        results = sorted(p.communicate(timeout=30)[0].strip() for p in procs)
        self.assertEqual(results.count("promoted"), 1, results)
        self.assertEqual(self.shared_keys(), {"ab" + "e" * 38})

    def test_green_layer_is_discarded_when_the_vm_never_goes_away(self) -> None:
        layer = self.attach("vm-stuck")
        write_entry(layer / "remote", "ab" + "f" * 38, entry(0))
        self.settle("vm-stuck")
        outcome = cl.process(self.layout, "vm-stuck", lambda _vm: True,
                             vm_gone_timeout=0.05, poll=0.01)
        self.assertEqual(outcome, "discarded")
        self.assertEqual(self.shared_keys(), set())

    def test_unreadable_inventory_is_never_taken_as_absence(self) -> None:
        def broken(_vm: str) -> bool:
            raise ValueError("tart list failed")

        self.assertFalse(cl.wait_vm_gone("vm", broken, timeout=0.05, poll=0.01))


class HostileLayerTests(Base):
    """A sibling guest can rewrite a green layer while the host promotes it."""

    def test_a_parent_swapped_after_header_validation_cannot_publish_host_data(self) -> None:
        for kind in (0, 1):
            with self.subTest(kind=kind):
                layer = self.attach(f"vm-parent-{kind}")
                key = "ab" + str(kind) * 38
                src = write_entry(layer / "remote", key, entry(kind))
                outside = self.tmp / f"outside-{kind}"
                outside.mkdir()
                (outside / src.name).write_bytes(b"fixture private host data")
                classify = cl.entry_type

                def swap_parent(path: Path) -> int | None:
                    result = classify(path)
                    path.parent.rename(path.parent.with_name("retired"))
                    path.parent.symlink_to(outside, target_is_directory=True)
                    return result

                with mock.patch.object(cl, "entry_type", side_effect=swap_parent):
                    counts = cl.promote_layer(self.layout, layer)
                self.assertEqual(counts["rejected"], 1)
                self.assertEqual(counts["promoted"], 0)
                self.assertFalse((self.layout.shared / key[:2] / key[2:]).exists())

    def test_copy_and_header_reads_reject_symlinked_ancestors(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        secret = outside / "entry"
        secret.write_bytes(entry(0, b"fixture private host data"))
        parent = self.tmp / "swapped-parent"
        parent.symlink_to(outside, target_is_directory=True)
        self.assertIsNone(cl.entry_type(parent / "entry"))
        with self.assertRaises(OSError):
            cl.copy_synced(parent / "entry", self.tmp / "copied")
        self.assertFalse((self.tmp / "copied").exists())

    def test_symlinked_destination_cannot_write_outside_the_share(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        self.layout.ensure()
        (self.layout.shared / "ab").symlink_to(outside, target_is_directory=True)
        for kind in (0, 1):
            with self.subTest(kind=kind):
                layer = self.attach(f"vm-dest-{kind}")
                write_entry(layer / "remote", "ab" + str(kind) * 38, entry(kind))
                counts = cl.promote_layer(self.layout, layer)
                self.assertEqual(counts["rejected"], 1)
                self.assertEqual(list(outside.iterdir()), [])
        src = self.tmp / "regular"
        src.write_bytes(entry(0))
        with self.assertRaises(OSError):
            cl.copy_synced(src, self.layout.shared / "ab" / "copied")
        self.assertEqual(list(outside.iterdir()), [])

    def test_publication_keeps_open_directories_when_both_parents_are_swapped(self) -> None:
        for manifest in (False, True):
            for copy_fallback in (False, True):
                with self.subTest(manifest=manifest, copy_fallback=copy_fallback):
                    case = self.tmp / f"race-{manifest}-{copy_fallback}"
                    source, target = case / "source", case / "target"
                    outside_source, outside_target = case / "outside-source", case / "outside-target"
                    for directory in (source, target, outside_source, outside_target):
                        directory.mkdir(parents=True)
                    payload = entry(int(manifest), b"guest cache")
                    src, dest = source / "entry", target / "entry"
                    src.write_bytes(payload)
                    os.utime(src, (3000, 3000))
                    if manifest:
                        dest.write_bytes(entry(1, b"old cache"))
                        os.utime(dest, (1000, 1000))
                    secret = outside_source / "entry"
                    secret.write_bytes(b"fixture private host data")
                    unrelated = outside_target / "entry"
                    unrelated.write_bytes(b"fixture unrelated host data")
                    real_link = cl.os.link
                    first = True

                    def swap_parents(*args, **kwargs):
                        nonlocal first
                        if first:
                            first = False
                            source.rename(case / "retired-source")
                            source.symlink_to(outside_source, target_is_directory=True)
                            target.rename(case / "retired-target")
                            target.symlink_to(outside_target, target_is_directory=True)
                            if copy_fallback:
                                raise OSError(errno.EXDEV, "fixture cross-device link")
                        return real_link(*args, **kwargs)

                    with mock.patch.object(cl.os, "link", side_effect=swap_parents):
                        if manifest:
                            outcome = cl.publish_manifest(src, dest, self.layout.locks / "race.lock")
                        else:
                            outcome = cl.publish_result(src, dest)
                    self.assertEqual(outcome, "replaced" if manifest else "promoted")
                    published = case / "retired-target" / "entry"
                    self.assertEqual(published.read_bytes(), payload)
                    if manifest:
                        self.assertEqual(published.stat().st_mtime_ns, 3000_000_000_000)
                    self.assertEqual(secret.read_bytes(), b"fixture private host data")
                    self.assertEqual(unrelated.read_bytes(), b"fixture unrelated host data")
                    self.assertEqual(list(outside_target.iterdir()), [unrelated])
                    self.assertFalse(list((case / "retired-target").glob(f"{cl.TMP_PREFIX}*")))

    def test_a_leaf_swapped_after_open_is_rejected_before_publication(self) -> None:
        src = self.tmp / "entry"
        src.write_bytes(entry(0))
        outside = self.tmp / "secret"
        outside.write_bytes(b"fixture private host data")
        dest = self.tmp / "published"
        real_link = cl.os.link

        def swap_leaf(*args, **kwargs):
            src.unlink()
            src.symlink_to(outside)
            return real_link(*args, **kwargs)

        with mock.patch.object(cl.os, "link", side_effect=swap_leaf), self.assertRaises(OSError):
            cl.publish_result(src, dest)
        self.assertFalse(dest.exists() or dest.is_symlink())
        self.assertEqual(outside.read_bytes(), b"fixture private host data")

    def test_trim_cannot_unlink_host_files_when_a_cache_parent_is_swapped(self) -> None:
        key = "ab" + "1" * 38
        cached = write_entry(self.layout.shared, key, entry(0, b"x" * 100), mtime=1)
        outside = self.tmp / "outside"
        outside.mkdir()
        secret = outside / cached.name
        secret.write_bytes(b"fixture private host data")
        scan = cl.os.scandir
        scans = 0

        def swap_before_scan(path):
            nonlocal scans
            # Swap after the shared directory listing, before its child is read.
            scans += 1
            if scans == 2:
                cached.parent.rename(cached.parent.with_name("retired"))
                cached.parent.symlink_to(outside, target_is_directory=True)
            return scan(path)

        with mock.patch.object(cl.os, "scandir", side_effect=swap_before_scan):
            cl.trim(self.layout, max_size=1, interval=0, now=10_000)
        self.assertGreaterEqual(scans, 2)
        self.assertEqual(secret.read_bytes(), b"fixture private host data")

    def test_trim_never_touches_a_symlinked_stamp_target(self) -> None:
        self.layout.ensure()
        secret = self.tmp / "secret"
        secret.write_bytes(b"fixture private host data")
        os.utime(secret, (1000, 1000))
        (self.layout.shared / cl.TRIM_STAMP).symlink_to(secret)
        try:
            cl.trim(self.layout, max_size=1, interval=0)
        except OSError:
            pass  # Refusing an unsafe stamp is also a safe outcome.
        self.assertEqual(secret.stat().st_mtime_ns, 1000_000_000_000)

    def test_an_entry_swapped_for_a_symlink_never_publishes_its_target(self) -> None:
        secret = self.tmp / "host-secret"
        secret.write_bytes(entry(0, b"host private key"))
        src = self.tmp / "swapped"
        src.symlink_to(secret)
        dest = self.tmp / "published"
        with self.assertRaises(OSError):
            cl.publish_result(src, dest)
        self.assertFalse(dest.exists() or dest.is_symlink())
        self.assertIsNone(cl.entry_type(src))
        with self.assertRaises(OSError):
            cl.copy_synced(src, self.tmp / "copied")

    def test_a_fifo_is_rejected_without_blocking_the_promoter(self) -> None:
        fifo = self.tmp / "fifo"
        os.mkfifo(fifo)
        self.assertIsNone(cl.entry_type(fifo))

    def test_layer_stats_never_reads_a_guest_written_config(self) -> None:
        seen: dict[str, str] = {}

        def fake_run(argv, env, **_kwargs):
            seen.update(env)
            return subprocess.CompletedProcess(argv, 0, "", "")

        original = cl.subprocess.run
        cl.subprocess.run = fake_run
        try:
            cl.layer_stats(self.tmp, "ccache")
        finally:
            cl.subprocess.run = original
        self.assertEqual(seen["CCACHE_CONFIGPATH"], os.devnull)
        self.assertEqual(seen["CCACHE_DIR"], str(self.tmp / "local"))


class KilledJobRegressionTests(Base):
    """A zero-include manifest from a job torn down mid-build never reaches shared.

    The 2026-09-26 m3 incident: VMs killed mid-build during a Tart hang left
    direct-mode manifests with no include paths in the shared cache, and every
    later build linked the wrong objects.
    """

    ZERO_INCLUDE_MANIFEST = b"\xcc\xac\x01\x01\x00\x00\x00\x00\x00\x00\x00\x6a\xb8\x6f\x72"

    def plant(self, vm: str) -> Path:
        layer = self.attach(vm, owner=os.getpid())
        write_entry(layer / "remote", "6c" + "0" * 38, self.ZERO_INCLUDE_MANIFEST)
        return layer

    def test_killed_timed_out_and_signalled_jobs_never_promote(self) -> None:
        scenarios = {
            "vm-timeout": dict(rc=124, log=self.log),
            "vm-signal": dict(rc=0, quarantine="signal_teardown_unknown", log=self.log),
            "vm-no-line": dict(rc=0, log=self.tmp / "absent.log"),
        }
        for vm, kwargs in scenarios.items():
            with self.subTest(vm):
                self.plant(vm)
                rc = kwargs.pop("rc")
                self.settle(vm, rc, **kwargs)
        cl.sweep(self.layout, never_present)
        self.assertEqual(self.shared_keys(), set())

    def test_orphaned_layer_of_a_dead_supervisor_is_discarded_not_promoted(self) -> None:
        dead = subprocess.Popen(["true"])
        dead.wait()
        layer = self.plant("vm-orphan")
        (self.layout.owners / "vm-orphan").write_text(f"{dead.pid}\n")
        live = self.plant("vm-live")
        counts = cl.sweep(self.layout, never_present)
        self.assertEqual(counts["orphaned"], 1)
        self.assertFalse(layer.exists())
        self.assertTrue(live.exists(), "a live owner's layer must survive a sweep")
        self.assertEqual(self.shared_keys(), set())


class SweepAndTrimTests(Base):
    def test_sweep_resumes_an_interrupted_green_promotion(self) -> None:
        layer = self.attach("vm-resume")
        write_entry(layer / "remote", "ab" + "a" * 38, entry(0))
        self.settle("vm-resume")
        counts = cl.sweep(self.layout, never_present)
        self.assertEqual(counts["resumed"], 1)
        self.assertEqual(self.shared_keys(), {"ab" + "a" * 38})

    def test_trim_evicts_oldest_until_under_the_cap_and_reaps_stale_tmp(self) -> None:
        for i in range(10):
            write_entry(self.layout.shared, f"{i:02x}" + "b" * 38, entry(0, b"x" * 1000),
                        mtime=1000.0 + i)
        stale = self.layout.shared / "00" / f"{cl.TMP_PREFIX}junk"
        stale.write_bytes(b"x")
        os.utime(stale, (1.0, 1.0))
        result = cl.trim(self.layout, max_size=5000, interval=0, now=10_000.0)
        self.assertEqual(result["removed_tmp"], 1)
        remaining = sorted(self.shared_keys())
        self.assertLessEqual(result["bytes_after"], 4500)
        self.assertEqual(remaining[0][:2], f"{10 - len(remaining):02x}")

    def test_parse_size(self) -> None:
        self.assertEqual(cl.parse_size("40G"), 40 * 1024 ** 3)
        self.assertEqual(cl.parse_size("5M"), 5 * 1024 ** 2)


@unittest.skipUnless(shutil.which("ccache") and shutil.which("cc"), "needs a real ccache and cc")
class RealCcacheTests(Base):
    """The guest configuration, run against a real ccache, end to end."""

    def compile(self, vm: str, source: Path) -> dict[str, int]:
        layer = self.layout.jobs / vm
        env = dict(os.environ,
                   CCACHE_DIR=str(layer / "local"), CCACHE_REMOTE_ONLY="true",
                   CCACHE_REMOTE_STORAGE=f"file:{self.layout.shared}|read-only "
                                         f"file:{layer / 'remote'}",
                   CCACHE_NODEPEND="true", CCACHE_COMPILERCHECK="content")
        subprocess.run(["ccache", "cc", "-c", str(source), "-o", str(self.tmp / "out.o")],
                       env=env, check=True, cwd=self.tmp)
        return cl.layer_stats(layer, shutil.which("ccache")) or {}

    def test_promoted_entries_serve_the_next_job_and_shared_stays_read_only(self) -> None:
        (self.tmp / "h.h").write_text("#define X 3\n")
        source = self.tmp / "a.c"
        source.write_text('#include "h.h"\nint f(void){return X;}\n')
        self.layout.ensure()
        before = set(self.layout.shared.rglob("*"))
        self.attach("vm-cold")
        cold = self.compile("vm-cold", source)
        self.assertEqual(set(self.layout.shared.rglob("*")), before,
                         "a guest compile must never write the shared store")
        self.assertEqual(cold.get("cache_miss"), 1)
        self.settle("vm-cold")
        self.assertEqual(cl.process(self.layout, "vm-cold", never_present), "promoted")
        self.assertEqual(len(self.shared_keys()), 2)
        self.attach("vm-warm")
        warm = self.compile("vm-warm", source)
        self.assertEqual(warm.get("direct_cache_hit"), 1, warm)
        self.assertEqual(list((self.layout.jobs / "vm-warm" / "remote").glob("*/*")), [],
                         "a remote hit must not be re-written into the job layer")


class ShellLibTests(unittest.TestCase):
    def fragments(self, value: str) -> subprocess.CompletedProcess[str]:
        script = textwrap.dedent(f"""
            set -euo pipefail
            TARTCI_ROOT={ROOT}; CACHE_ROOT=/tmp/cache-root
            TARTCI_CCACHE_WRITE_ISOLATION='{value}'
            source {LIB}
            tartci_ccache_layer_guest_fragments vm-x
            printf 'PREP<%s>\\nENV<%s>\\nROOT<%s>\\n' "$CCACHE_LAYER_GUEST_PREP" "$CCACHE_LAYER_GUEST_ENV" "$CCACHE_LAYER_ROOT"
        """)
        return subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)

    def test_unset_and_zero_produce_no_guest_change(self) -> None:
        for value in ("", "0"):
            with self.subTest(value=value):
                result = self.fragments(value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("PREP<>\nENV<>", result.stdout)

    def test_enabled_points_the_guest_at_its_own_layer_and_a_read_only_share(self) -> None:
        result = self.fragments("1")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("ROOT</tmp/cache-root/ccache/tartci-layers-v1>", out)
        self.assertIn("'/Volumes/My Shared Files/ccache/tartci-layers-v1/jobs/vm-x'", out)
        self.assertIn('file:$HOME/.tartci-ccache/shared|read-only file:$HOME/.tartci-ccache/job/remote', out)
        self.assertIn("CCACHE_REMOTE_ONLY=true", out)
        self.assertIn("CCACHE_REMOTE_STORAGE=file:%s|read-only file:%s", out)

    def test_invalid_value_fails_closed(self) -> None:
        result = self.fragments("yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid TARTCI_CCACHE_WRITE_ISOLATION", result.stderr)

    def test_runner_splices_fragments_without_changing_the_legacy_command(self) -> None:
        source = RUNNER.read_text()
        self.assertIn("ln -sfn '/Volumes/My Shared Files/ccache' ~/Library/Caches/ccache && \\\n"
                      "     ${CCACHE_LAYER_GUEST_PREP}export CCACHE_NODEPEND=true", source)
        self.assertIn("     ${CCACHE_LAYER_GUEST_ENV}mv .env.tartci .env && \\", source)
        wrapper = source[source.index("run_runner_until_done(){"):
                         source.index("run_runner_until_done_unlayered(){")]
        # The prepared driver bypasses host-cache layers. Check the call that
        # runs within the legacy attach/settle path, independent of that bypass.
        legacy_run = wrapper.index('run_runner_until_done_unlayered "$@" || layer_rc=$?')
        self.assertLess(wrapper.index("tartci_ccache_layer_attach"), legacy_run)
        self.assertLess(legacy_run,
                        wrapper.index("tartci_ccache_layer_settle"))


class ProfileTests(unittest.TestCase):
    def test_no_shipped_profile_enables_isolation(self) -> None:
        for path in sorted((ROOT / "profiles").glob("*-macos-fleet.toml")):
            with self.subTest(profile=path.name):
                data = fleet.load(path)
                for lane in data["lane"]:
                    self.assertNotIn("ccache_write_isolation", lane)
                for name, body in fleet.rendered_plists(data).items():
                    self.assertNotIn(b"TARTCI_CCACHE_WRITE_ISOLATION", body, name)

    def profile_with(self, value: str) -> Path:
        base = (ROOT / "profiles" / "m5-macos-fleet.toml").read_text()
        marker = 'id = "pulp-gate"\n'
        text = base.replace(marker, marker + f"ccache_write_isolation = {value}\n", 1)
        tomllib.loads(text)
        handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return Path(handle.name)

    def test_enabled_lane_renders_the_flag_for_that_lane_only(self) -> None:
        data = fleet.load(self.profile_with("true"))
        rendered = fleet.rendered_plists(data)
        flagged = sorted(name for name, body in rendered.items()
                         if b"TARTCI_CCACHE_WRITE_ISOLATION" in body)
        self.assertTrue(flagged)
        self.assertTrue(all("pulp-gate" in name for name in flagged), flagged)

    def test_non_boolean_is_rejected(self) -> None:
        with self.assertRaises((ValueError, SystemExit)):
            fleet.load(self.profile_with('"yes"'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
