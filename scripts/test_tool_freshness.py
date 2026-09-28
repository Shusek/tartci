#!/usr/bin/env python3
"""Hermetic tests for tool freshness, its status surfaces, and the heal pass.

Every tool run and feed read is a fake, so nothing here touches the real
Shipyard or pulp installs or the network. Each alarm is exercised with the
fault present and absent, so a check that can only pass would fail here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tool_freshness as tf  # noqa: E402

HOUR = 3600.0
NOW = 1790000000.0  # 2026-09-21T13:33:20Z


def iso(ts: float) -> str:
    return tf._iso(ts)


def feed(*releases: tuple[str, float]) -> str:
    """An Atom feed like GitHub's, newest first, with a second tag family."""
    entries = "".join(
        f"""<entry><updated>{iso(at)}</updated>
        <link rel="alternate" type="text/html" href="https://github.com/o/r/releases/tag/{tag}"/>
        <title>{tag}</title></entry>"""
        for tag, at in sorted(releases, key=lambda r: -r[1]))
    plugin = f"""<entry><updated>{iso(NOW)}</updated>
        <link rel="alternate" href="https://github.com/o/r/releases/tag/plugin-v9.9.9"/></entry>"""
    return f'<feed xmlns="http://www.w3.org/2005/Atom">{plugin}{entries}</feed>'


class Host:
    """Fake tool installs: CLI versions, the ghapp generation, fleet-update, a call log."""

    def __init__(self, versions: dict[str, str | None], generation: str | None = None):
        self.versions = versions
        self.generation = generation
        self.calls: list[list[str]] = []
        self.apply_installs: dict[str, str | None] = {}
        self.apply_generation: str | None = None  # default: the generation moves with the CLI
        self.verdict = "verified"

    def run(self, argv: list[str], timeout: float) -> tuple[int, str]:
        self.calls.append(argv)
        if "auth-generations" in argv[0]:
            return (0, f"shipyard {self.generation}") if self.generation else (1, "exec failed")
        name = Path(argv[0]).name
        if "fleet-update" in argv:
            installed = self.apply_installs.get(name)
            if installed is None:
                return 1, json.dumps({"event": "fleet_summary", "verdict": "failed"}, indent=2)
            self.versions[name] = installed
            if self.generation is not None:
                self.generation = self.apply_generation or installed
            receipt = {"event": "host_verification", "host_class": argv[argv.index("--host-class") + 1],
                       "verdict": self.verdict}
            summary = {"event": "fleet_summary", "target": argv[argv.index("--to") + 1],
                       "verdict": self.verdict}
            # Shipyard streams one pretty-printed document per event.
            return 0, json.dumps(receipt, indent=2) + "\n" + json.dumps(summary, indent=2)
        version = self.versions.get(name)
        if version is None:
            return 127, "not installed"
        return 0, f"{name} {version}" if name == "shipyard" else f"pulp v{version}"


def make_generation(home: Path) -> Path:
    """The layout Shipyard installs: a symlink to ghapp inside a generation dir."""
    gen = home / ".local/share/shipyard/auth-generations/75f602aa0abf"
    gen.mkdir(parents=True)
    (gen / "ghapp").write_text("#!/bin/sh\n")
    link = home / ".local/bin/ghapp.shipyard-generation"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(gen / "ghapp")
    return gen


def settings(**overrides) -> dict:
    value = {"stale_hours": 12.0, "apply_after_minutes": 30.0, "apply_retry_hours": 6.0,
             "host_class": "studio",
             "tools": {name: dict(tool, enabled=True) for name, tool in tf.DEFAULT_TOOLS.items()}}
    value.update(overrides)
    return value


class ToolFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.home / ".tartci")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = tf.state_dir_for(self.home)
        self.feeds = {
            "danielraffel/Shipyard": feed(("v0.221.0", NOW - 30 * HOUR), ("v0.221.1", NOW - 20 * HOUR)),
            "Generous-Corp/pulp": feed(("v0.877.1", NOW - 3 * HOUR), ("v0.877.2", NOW - 1 * HOUR)),
        }

    def feed(self, repo: str) -> str:
        return self.feeds[repo]

    def refresh(self, host: Host, now: float = NOW, **kw) -> dict:
        return tf.refresh(self.home, now, settings=kw.pop("settings", settings()),
                          run=host.run, feed=self.feed, **kw)

    def events(self) -> list[dict]:
        path = self.state / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_current_tools_raise_no_problem(self) -> None:
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2"})
        value = self.refresh(host)
        self.assertEqual({row["state"] for row in value["tools"].values()}, {"current"})
        summary = tf.summary(self.home)
        self.assertIsNone(summary["problem"])
        self.assertIn("shipyard: 0.221.1 current with latest v0.221.1", "\n".join(summary["lines"]))

    def test_behind_is_timed_from_the_first_newer_release_and_goes_stale(self) -> None:
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.1"})
        tools = self.refresh(host, settings=settings(tools={
            name: dict(tool, enabled=True, auto_apply=False)
            for name, tool in tf.DEFAULT_TOOLS.items()}))["tools"]
        # Shipyard: v0.221.1 was published 20 h ago, so 20 h behind: STALE at 12 h.
        self.assertEqual(tools["shipyard"]["behind_hours"], 20.0)
        self.assertTrue(tools["shipyard"]["stale"])
        # pulp: v0.877.2 is 1 h old: behind, not stale. The plugin-v9.9.9 tag is
        # another release family and must not count as newer.
        self.assertEqual(tools["pulp"]["latest_tag"], "v0.877.2")
        self.assertEqual(tools["pulp"]["releases_behind"], 1)
        self.assertFalse(tools["pulp"]["stale"])
        summary = tf.summary(self.home)
        self.assertEqual(summary["problem"], "shipyard 20 h behind v0.221.1")
        self.assertIn("STALE", "\n".join(summary["lines"]))

    def test_an_install_older_than_the_feed_is_a_lower_bound_that_never_moves_later(self) -> None:
        no_apply = settings(tools={name: dict(tool, enabled=True, auto_apply=False)
                                   for name, tool in tf.DEFAULT_TOOLS.items()})
        host = Host({"shipyard": "0.221.1", "pulp": "0.305.0"})
        first = self.refresh(host, settings=no_apply)["tools"]["pulp"]
        self.assertTrue(first["behind_since_lower_bound"])
        self.assertEqual(first["behind_since"], iso(NOW - 3 * HOUR))
        # The feed scrolls: its oldest entry is now newer. The recorded bound stays.
        self.feeds["Generous-Corp/pulp"] = feed(("v0.877.2", NOW - HOUR), ("v0.878.0", NOW + HOUR))
        later = self.refresh(host, NOW + 2 * HOUR, settings=no_apply)["tools"]["pulp"]
        self.assertEqual(later["behind_since"], iso(NOW - 3 * HOUR))
        self.assertEqual(later["behind_hours"], 5.0)
        self.assertIn(">=5 h", tf.render_row(later))

    def test_missing_binary_is_reported_but_not_a_problem(self) -> None:
        self.refresh(Host({"shipyard": "0.221.1", "pulp": None}))
        summary = tf.summary(self.home)
        self.assertIsNone(summary["problem"])
        self.assertIn("pulp: not installed", "\n".join(summary["lines"]))

    def test_unreadable_feed_or_version_is_unknown_and_a_problem(self) -> None:
        def broken(repo: str) -> str:
            raise OSError("network down")
        tf.refresh(self.home, NOW, settings=settings(), feed=broken,
                   run=Host({"shipyard": "0.221.1", "pulp": "0.877.2"}).run)
        summary = tf.summary(self.home)
        self.assertEqual(summary["problem"], "pulp freshness unknown; shipyard freshness unknown")
        self.assertIn("UNKNOWN (releases unreadable: OSError: network down)",
                      "\n".join(summary["lines"]))

    def test_auto_apply_waits_for_a_young_release(self) -> None:
        self.feeds["danielraffel/Shipyard"] = feed(("v0.221.0", NOW - 30 * HOUR),
                                                   ("v0.221.1", NOW - 600))
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"})
        host.apply_installs["shipyard"] = "0.221.1"
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertEqual(row["state"], "behind")
        self.assertIn("waiting", row["apply"])
        self.assertFalse(any("fleet-update" in call for call in host.calls))

    def test_auto_apply_installs_verifies_and_records_one_deploy_event(self) -> None:
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"})
        host.apply_installs["shipyard"] = "0.221.1"
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertIn([str(self.home / ".local/bin/shipyard"), "runner", "fleet-update", "--to",
                       "v0.221.1", "--host-class", "studio", "--apply", "--json"], host.calls)
        self.assertEqual(row["state"], "current")
        self.assertEqual(row["apply"], "applied v0.221.1: ok")
        self.assertEqual(self.events(), [{
            "event": "tool_deployed", "tool": "shipyard", "component": "cli", "at": iso(NOW),
            "from": "0.221.0", "to": "0.221.1", "latest": "0.221.1", "by": "auto_apply",
            "downgrade": False, "verify": "current"}])
        # pulp has no apply command: it is never updated from here.
        self.assertFalse(any(Path(c[0]).name == "pulp" and len(c) > 2 for c in host.calls))

    def test_a_lagging_ghapp_generation_makes_a_current_cli_stale(self) -> None:
        make_generation(self.home)
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2"}, generation="0.221.0")
        off = settings(tools={name: dict(tool, enabled=True, auto_apply=False)
                              for name, tool in tf.DEFAULT_TOOLS.items()})
        row = self.refresh(host, settings=off)["tools"]["shipyard"]
        self.assertEqual((row["installed"], row["generation"], row["effective"]),
                         ("0.221.1", "0.221.0", "0.221.0"))
        self.assertEqual(row["state"], "behind")
        self.assertTrue(row["stale"])
        line = tf.render_row(row)
        self.assertIn("0.221.1 (ghapp generation 0.221.0) behind latest v0.221.1", line)
        self.assertIn("STALE", line)
        self.assertEqual(tf.summary(self.home)["problem"], "shipyard 20 h behind v0.221.1")
        host.generation = "0.221.1"
        row = self.refresh(host, NOW + HOUR, settings=off)["tools"]["shipyard"]
        self.assertEqual(row["state"], "current")
        self.assertEqual([(e["component"], e["from"], e["to"], e["by"]) for e in self.events()],
                         [("auth_generation", "0.221.0", "0.221.1", "observed")])

    def test_an_unreadable_generation_is_unknown_not_current(self) -> None:
        make_generation(self.home)
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2"}, generation=None)
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertEqual(row["state"], "unknown")
        self.assertIn("generation 75f602aa0abf version unreadable", row["reason"])

    def test_fleet_update_moves_cli_and_generation_and_logs_both(self) -> None:
        make_generation(self.home)
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2"}, generation="0.221.0")
        host.apply_installs["shipyard"] = "0.221.1"
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertEqual(row["apply"], "applied v0.221.1: ok")
        self.assertEqual((row["state"], row["generation"]), ("current", "0.221.1"))
        self.assertEqual([(e["component"], e["from"], e["to"], e["by"]) for e in self.events()],
                         [("auth_generation", "0.221.0", "0.221.1", "auto_apply")])

    def test_a_fleet_update_verdict_other_than_verified_is_a_failure(self) -> None:
        make_generation(self.home)
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"}, generation="0.221.0")
        host.apply_installs["shipyard"] = "0.221.1"
        host.verdict = "rollback_failed"
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertIn("FAILED (verdict rollback_failed", row["apply"])
        failed = [e for e in self.events() if e["event"] == "tool_apply_failed"]
        self.assertEqual([e["verdict"] for e in failed], ["rollback_failed"])

    def test_a_verified_verdict_with_the_generation_left_behind_is_a_failure(self) -> None:
        make_generation(self.home)
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"}, generation="0.221.0")
        host.apply_installs["shipyard"] = "0.221.1"
        host.apply_generation = "0.221.0"
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertIn("FAILED (verdict verified", row["apply"])
        self.assertIn("generation 0.221.0", row["apply"])

    def test_no_host_class_refuses_to_apply(self) -> None:
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"})
        host.apply_installs["shipyard"] = "0.221.1"
        no_class = settings()
        del no_class["host_class"]
        row = self.refresh(host, settings=no_class)["tools"]["shipyard"]
        self.assertIn("refused: no host class", row["apply"])
        self.assertFalse(any("fleet-update" in call for call in host.calls))

    def test_host_class_defaults_to_the_fleet_profile_host_id(self) -> None:
        if tf.tomllib is None:
            self.skipTest("needs tomllib")
        profile = self.home / ".config/tartci/macos-fleet-profile.toml"
        profile.parent.mkdir(parents=True)
        profile.write_text('[host]\nid = "m5"\nssh = "m5"\n')
        self.assertEqual(tf.host_class(self.home, {}), "m5")
        self.assertEqual(tf.host_class(self.home, {"host_class": "m1"}), "m1")

    def test_a_failed_apply_is_recorded_and_not_retried_inside_the_window(self) -> None:
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"})
        row = self.refresh(host)["tools"]["shipyard"]
        self.assertIn("FAILED", row["apply"])
        self.assertEqual([e["event"] for e in self.events()], ["tool_apply_failed"])
        calls = len([c for c in host.calls if "fleet-update" in c])
        row = self.refresh(host, NOW + HOUR)["tools"]["shipyard"]
        self.assertEqual(len([c for c in host.calls if "fleet-update" in c]), calls)
        self.assertIn("already attempted v0.221.1", row["apply"])
        self.refresh(host, NOW + 7 * HOUR)
        self.assertEqual(len([c for c in host.calls if "fleet-update" in c]), calls + 1)

    def test_auto_apply_off_leaves_the_tool_alone(self) -> None:
        host = Host({"shipyard": "0.221.0", "pulp": "0.877.2"})
        host.apply_installs["shipyard"] = "0.221.1"
        off = settings(tools={name: dict(tool, enabled=True, auto_apply=False)
                              for name, tool in tf.DEFAULT_TOOLS.items()})
        self.refresh(host, settings=off)
        self.assertFalse(any("fleet-update" in call for call in host.calls))

    def test_a_deployment_made_elsewhere_is_observed_and_recorded(self) -> None:
        host = Host({"shipyard": "0.221.1", "pulp": "0.305.0"})
        self.refresh(host)
        host.versions["pulp"] = "0.877.2"
        self.refresh(host, NOW + HOUR)
        self.assertEqual([(e["tool"], e["from"], e["to"], e["by"], e["verify"])
                          for e in self.events()],
                         [("pulp", "0.305.0", "0.877.2", "observed", "current")])

    def test_if_older_skips_a_fresh_cache_and_summary_never_runs_a_tool(self) -> None:
        host = Host({"shipyard": "0.221.1", "pulp": "0.877.2"})
        self.refresh(host)
        before = len(host.calls)
        self.refresh(host, NOW + 60, if_older=1800)
        self.assertEqual(len(host.calls), before)
        self.refresh(host, NOW + 1900, if_older=1800)
        self.assertGreater(len(host.calls), before)
        with mock.patch.object(tf, "run_command", side_effect=AssertionError("ran a tool")), \
                mock.patch.object(tf, "fetch_feed", side_effect=AssertionError("fetched")):
            self.assertEqual(len(tf.summary(self.home)["lines"]), 2)

    def test_a_disabled_tool_is_neither_run_nor_reported(self) -> None:
        host = Host({"shipyard": "0.221.1", "pulp": "0.305.0"})
        tools = dict(tf.DEFAULT_TOOLS)
        value = self.refresh(host, settings=settings(tools={
            "shipyard": dict(tools["shipyard"], enabled=True),
            "pulp": dict(tools["pulp"], enabled=False)}))
        self.assertEqual(sorted(value["tools"]), ["shipyard"])
        self.assertFalse(any(Path(call[0]).name == "pulp" for call in host.calls))

    def test_settings_file_overrides_defaults(self) -> None:
        if tf.tomllib is None:
            self.skipTest("needs tomllib")
        path = self.home / "tool-freshness.toml"
        path.write_text("stale_hours = 2\n[tools.pulp]\nenabled = false\n"
                        "[tools.shipyard]\nauto_apply = false\n")
        value = tf.load_settings(self.home, path)
        self.assertEqual(value["stale_hours"], 2.0)
        self.assertFalse(value["tools"]["pulp"]["enabled"])
        self.assertFalse(value["tools"]["shipyard"]["auto_apply"])
        self.assertEqual(value["tools"]["shipyard"]["repo"], "danielraffel/Shipyard")


class StatusSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def vitals(self, fsev: object) -> Path:
        path = self.dir / "host_vitals.json"
        reading = {"level": "green", "sampled_at": 1}
        if fsev is not None:
            reading["fseventsd"] = fsev
        path.write_text(json.dumps(reading))
        return path

    def test_fseventsd_over_its_limit_is_a_problem_and_under_is_not(self) -> None:
        import macos_fleet_lanes as lanes
        big = lanes.host_vitals_summary(self.vitals(
            {"rss_mb": 10854, "cpu_pct": 107.5, "warn": True, "warn_mb": 1024}))
        self.assertEqual(big["problem"], "fseventsd 10854 MB RSS > 1024 MB")
        self.assertIn("WARN (> 1024 MB; restart with sudo killall fseventsd)", big["lines"][0])
        small = lanes.host_vitals_summary(self.vitals(
            {"rss_mb": 20, "cpu_pct": 1.0, "warn": False, "warn_mb": 1024}))
        self.assertIsNone(small["problem"])
        self.assertTrue(small["lines"][0].startswith("fseventsd: 20 MB RSS, 1.0% CPU"))

    def test_fseventsd_absent_or_unpublished_reads_unknown_never_ok(self) -> None:
        import macos_fleet_lanes as lanes
        self.assertIn("UNKNOWN (host-vitals reading has no fseventsd field",
                      lanes.host_vitals_summary(self.vitals(None))["lines"][0])
        self.assertIn("UNKNOWN (no host-vitals reading",
                      lanes.host_vitals_summary(self.dir / "absent.json")["lines"][0])

    def test_status_lines_and_watchdog_warning_carry_both(self) -> None:
        import macos_fleet_lanes as lanes
        import tartci_launchd_watchdog as wd
        value = {"profile_drift": {"state": "in_sync"}, "supply": {"state": "match"},
                 "self_update": {"lines": ["tartci: current with main"], "problem": None},
                 "tool_freshness": {"lines": ["shipyard: 0.219.0 behind ... STALE"],
                                    "problem": "shipyard 20 h behind v0.221.1"},
                 "host_vitals": {"lines": ["fseventsd: 10854 MB RSS WARN"],
                                 "problem": "fseventsd 10854 MB RSS > 1024 MB"}}
        rendered = lanes.render_config_verdicts(value)
        self.assertIn("shipyard: 0.219.0 behind ... STALE", rendered)
        self.assertIn("fseventsd: 10854 MB RSS WARN", rendered)
        self.assertEqual(wd.config_problem(value),
                         "tool_freshness=shipyard 20 h behind v0.221.1; "
                         "host_vitals=fseventsd 10854 MB RSS > 1024 MB")
        clean = dict(value, tool_freshness={"lines": [], "problem": None},
                     host_vitals={"lines": [], "problem": None})
        self.assertIsNone(wd.config_problem(clean))

    def test_doctor_reports_stale_current_and_unmeasured(self) -> None:
        import fleet_doctor as fd
        self.assertEqual(fd.check_tool_freshness(None).code, "tool_freshness_unmeasured")
        self.assertEqual(fd.check_tool_freshness({"state": None}).code, "tool_freshness_unmeasured")
        stale = fd.check_tool_freshness({"state": {}, "lines": ["x"], "problem": "pulp 2800 h"})
        self.assertEqual((stale.code, stale.state), ("tool_freshness_stale", fd.PROBLEM))
        ok = fd.check_tool_freshness({"state": {}, "lines": ["x"], "problem": None})
        self.assertEqual((ok.code, ok.state), ("tool_freshness_current", fd.OK))
        for code in ("tool_freshness_current", "tool_freshness_stale", "tool_freshness_unmeasured"):
            self.assertIn(code, fd.CODES)


class HealPassTests(unittest.TestCase):
    """`tartci launchd heal` must run the watchdog even when the relay reconcile fails."""

    def run_heal(self, reconcile_rc: int) -> tuple[subprocess.CompletedProcess, Path]:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = tmp / "tartci-root"
        (root / "scripts").mkdir(parents=True)
        shutil.copy(HERE.parent / "tartci", root / "tartci")
        marker = tmp / "watchdog-ran"
        (root / "scripts" / "network_profile.py").write_text(textwrap.dedent(f"""\
            import sys
            print("network-profile: FAIL: network-profile reload deferred while a Tart VM is running")
            sys.exit({reconcile_rc})
            """))
        (root / "scripts" / "tartci_launchd_watchdog.py").write_text(textwrap.dedent(f"""\
            import pathlib, sys
            pathlib.Path({str(marker)!r}).write_text(" ".join(sys.argv[1:]))
            """))
        env = dict(os.environ, TARTCI_PYTHON=sys.executable, TARTCI_HOME=str(tmp / "home"))
        proc = subprocess.run(["bash", str(root / "tartci"), "launchd", "heal",
                               "--stale-log-seconds", "4500"],
                              capture_output=True, text=True, env=env, timeout=60)
        return proc, marker

    def test_a_deferred_reconcile_still_runs_the_watchdog_and_reports_why(self) -> None:
        if sys.version_info < (3, 11):
            self.skipTest("the tartci shim needs a tomllib interpreter")
        proc, marker = self.run_heal(1)
        self.assertTrue(marker.exists(), proc.stdout + proc.stderr)
        self.assertEqual(marker.read_text(), "--stale-log-seconds 4500")
        self.assertEqual(proc.returncode, 6)
        self.assertIn("reload deferred while a Tart VM is running", proc.stdout)

    def test_a_clean_reconcile_runs_the_watchdog_quietly(self) -> None:
        if sys.version_info < (3, 11):
            self.skipTest("the tartci shim needs a tomllib interpreter")
        proc, marker = self.run_heal(0)
        self.assertTrue(marker.exists(), proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("network-profile", proc.stdout)


if __name__ == "__main__":
    unittest.main()
