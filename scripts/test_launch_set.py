#!/usr/bin/env python3
"""launch_set: building, signing, verifying and pointing at a launch set."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import launch_set as ls

TEAM = "AB12CD34EF"
STORE = "/Volumes/Example/VMs"


HERE = Path(__file__).resolve().parent
PIN = "/usr/bin/python3"


def make_generation(root: Path, name: str = "a" * 40 + "-1", *, pin: str = PIN,
                    tartci: str = "#!/bin/bash\nexit 0\n") -> Path:
    """A support generation the real `tartci_support_manifest.py verify
    --immutable` accepts once frozen, with v2's entrypoint and shim."""
    generation = root / name
    (generation / "scripts").mkdir(parents=True)
    members = []
    for relative, data, mode in (
            ("tartci", tartci.encode(), 0o755),
            ("scripts/x.py", b"print(1)\n", 0o644),
            ("scripts/tartci_support_manifest.py",
             (HERE / "tartci_support_manifest.py").read_bytes(), 0o644),
            ("scripts/python_child.py", (HERE / "python_child.py").read_bytes(), 0o644)):
        path = generation / relative
        path.write_bytes(data)
        os.chmod(path, mode)
        members.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "mode": mode})
    (generation / ls.MANIFEST_NAME).write_text(json.dumps(
        {"schema": 2, "repository": "https://github.com/danielraffel/tartci.git",
         "source_commit": "a" * 40, "members": members}))
    os.chmod(generation / ls.MANIFEST_NAME, 0o444)
    ls.write_runtime(generation, pin)
    return generation


LANES = {"studio-pulp-gate": {"environment": {"TART_HOME": STORE, "PATH": "/usr/bin:/bin",
                                              "TARTCI_PYTHON": PIN}}}


class FakeCodesign:
    """codesign stand-in: signing writes a digest of the set; verification
    accepts only that digest and the pinned requirement."""

    def __init__(self, *, sign_rc: int = 0) -> None:
        self.sign_rc = sign_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        target = Path(argv[-1])
        sig = Path(argv[argv.index("--detached") + 1])
        if "--sign" in argv:
            if self.sign_rc == 0:
                sig.write_text(hashlib.sha256(target.read_bytes()).hexdigest())
            return subprocess.CompletedProcess(argv, self.sign_rc, "", "" if self.sign_rc == 0 else "no identity")
        ok = (sig.is_file() and sig.read_text() == hashlib.sha256(target.read_bytes()).hexdigest()
              and f"-R={ls.requirement(TEAM)}" in argv)
        return subprocess.CompletedProcess(argv, 0 if ok else 1, "", "" if ok else "invalid signature")


class LaunchSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        # Canonical, so the trust checks see the same path whatever TMPDIR is.
        self.root = Path(os.path.realpath(self.td.name)) / "generations"
        self.root.mkdir()
        os.chmod(self.root, 0o755)
        self.pointer = self.root.parent / "config" / "launcher-generation.json"
        self.generation = make_generation(self.root)

    def tearDown(self) -> None:
        for dirpath, dirs, _files in os.walk(self.td.name):
            for name in dirs:
                os.chmod(os.path.join(dirpath, name), 0o755)
        self.td.cleanup()

    def written(self, generation: Path | None = None, sequence: int = 2) -> Path:
        generation = generation or self.generation
        os.chmod(generation, 0o755)
        ls.write(generation, ls.build(generation, LANES, tart_home=STORE, python=PIN, sequence=sequence))
        return generation

    def publish(self, generation: Path, codesign=None, ready=lambda: None) -> dict:
        return ls.publish(generation, pointer=self.pointer, root=self.root, team_id=TEAM,
                          identity="Developer ID Application: Example", keychain="/k.keychain-db",
                          signing_ready=ready, run=codesign or FakeCodesign())

    def test_the_set_pins_the_manifest_entrypoint_and_lanes(self) -> None:
        value = ls.build(self.generation, LANES, tart_home=STORE, python=PIN, sequence=7)
        self.assertEqual(7, value["sequence"])
        self.assertEqual("a" * 40, value["source_commit"])
        self.assertEqual(hashlib.sha256((self.generation / ls.MANIFEST_NAME).read_bytes()).hexdigest(),
                         value["manifest_sha256"])
        self.assertEqual(".tartci-launch-v2", value["entrypoint"]["path"])
        shim = str(self.generation / "bin/python3")
        self.assertEqual({"pin": PIN, "shim": {"path": "bin/python3", "sha256": hashlib.sha256(
            (self.generation / "bin/python3").read_bytes()).hexdigest()}}, value["python"])
        # The lane's interpreter is rewritten to the shim; nothing else changes.
        self.assertEqual({**LANES["studio-pulp-gate"]["environment"], "TARTCI_PYTHON": shim},
                         value["lanes"]["studio-pulp-gate"]["environment"])

    def test_the_shim_execs_the_pin_isolated_and_the_entrypoint_uses_it(self) -> None:
        self.assertEqual(b'#!/bin/sh\nexec /usr/bin/python3 -B -E -s -S "$@"\n',
                         (self.generation / "bin/python3").read_bytes())
        entry = (self.generation / ls.ENTRYPOINT).read_text()
        self.assertIn(f"{self.generation}/bin/python3 {self.generation}/scripts/tartci_support_manifest.py", entry)
        self.assertNotIn("/usr/bin/python3", entry)
        # A Homebrew Cellar pin carries "@" and must be accepted.
        ls.shim_bytes("/opt/homebrew/Cellar/python@3.12/3.12.13_2/Frameworks/Python.framework/"
                      "Versions/3.12/bin/python3.12")
        for pin in ("python3", "/opt/x y/python3", "/opt/$(id)/python3", '/opt/"/python3'):
            with self.subTest(pin=pin), self.assertRaisesRegex(ls.LaunchSetError, "plain absolute"):
                ls.shim_bytes(pin)

    def test_a_lane_interpreter_other_than_the_pin_is_refused(self) -> None:
        for key in ("TARTCI_PYTHON", "SHIPYARD_GHAPP_PYTHON_BINARY"):
            lanes = {"x": {"environment": {"TART_HOME": STORE, key: "/opt/homebrew/bin/python3.12"}}}
            with self.subTest(key=key), self.assertRaisesRegex(ls.LaunchSetError, "not the pinned"):
                ls.build(self.generation, lanes, tart_home=STORE, python=PIN, sequence=1)

    def test_a_shim_or_entrypoint_not_matching_the_pin_is_refused(self) -> None:
        with self.assertRaisesRegex(ls.LaunchSetError, "bin/python3 is not the expected bytes"):
            ls.build(self.generation, LANES, tart_home=STORE, python="/opt/homebrew/bin/python3.11",
                     sequence=1)
        os.chmod(self.generation / ls.ENTRYPOINT, 0o755)
        (self.generation / ls.ENTRYPOINT).write_text("#!/bin/bash\nexec /usr/bin/python3\n")
        with self.assertRaisesRegex(ls.LaunchSetError, "tartci-launch-v2 is not the expected bytes"):
            ls.build(self.generation, LANES, tart_home=STORE, python=PIN, sequence=1)

    def test_sibling_imports_still_work_through_the_shim(self) -> None:
        # peer_stall_alert imports host_off from its own directory; -I (whose
        # -P drops that directory) would break it, the shim's flags must not.
        work = Path(os.path.realpath(self.td.name)) / "shimmed"
        work.mkdir()
        ls.write_runtime(work, sys.executable)
        script = HERE / "peer_stall_alert.py"
        result = subprocess.run([str(work / "bin/python3"), str(script)], capture_output=True,
                                text=True, timeout=120, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        if sys.version_info >= (3, 11):
            control = subprocess.run([sys.executable, "-P", "-S", str(script)], capture_output=True,
                                     text=True, timeout=120, check=False)
            self.assertNotEqual(0, control.returncode, "-P must break the sibling import")
            self.assertIn("ModuleNotFoundError", control.stderr)

    def test_a_lane_off_the_store_or_with_a_loader_variable_is_refused(self) -> None:
        for lanes in ({"x": {"environment": {"TART_HOME": "/Users/example/VMs"}}},
                      {"x": {"environment": {"TART_HOME": STORE, "DYLD_INSERT_LIBRARIES": "/x"}}},
                      {"../x": {"environment": {"TART_HOME": STORE}}}, {}):
            with self.subTest(lanes=lanes), self.assertRaises(ls.LaunchSetError):
                ls.build(self.generation, lanes, tart_home=STORE, python=PIN, sequence=1)
        for sequence in (0, -1, "2", 2.0, True):
            with self.subTest(sequence=sequence), self.assertRaisesRegex(ls.LaunchSetError, "sequence"):
                ls.build(self.generation, LANES, tart_home=STORE, python=PIN, sequence=sequence)

    def test_the_swift_requirement_is_the_python_requirement(self) -> None:
        # One source of truth in two languages: rebuild the launcher's
        # requirement from main.swift's string literals and compare.
        import re as _re
        swift = (Path(__file__).resolve().parents[1]
                 / "native/macos/tartci-launcher/main.swift").read_text()
        body = swift[swift.index("private func launchSetRequirement"):]
        body = body[body.index('return "anchor apple generic"'):]
        body = body[:body.index("\n}\n")]
        literals = _re.findall(r'"((?:[^"\\]|\\.)*)"', body)
        rebuilt = ("".join(literals).replace('\\"', '"')
                   .replace("\\(policy.team_id)", TEAM)
                   .replace("\\(launchSetIdentifier)", ls.LAUNCH_SET_IDENTIFIER))
        self.assertEqual(ls.requirement(TEAM), rebuilt)

    def test_the_requirement_pins_the_policy_team(self) -> None:
        self.assertEqual('anchor apple generic'
                         ' and certificate 1[field.1.2.840.113635.100.6.2.6] exists'
                         ' and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
                         ' and certificate leaf[subject.OU] = "AB12CD34EF"'
                         ' and identifier "com.danielraffel.tartci.launch-set"', ls.requirement(TEAM))
        for bad in ("ab12cd34ef", "AB12", 'X" or "1', ""):
            with self.subTest(bad=bad), self.assertRaises(ls.LaunchSetError):
                ls.requirement(bad)

    def test_signing_needs_the_dedicated_keychain(self) -> None:
        self.written()
        codesign = FakeCodesign()
        for keychain in (None, ""):
            with self.subTest(keychain=keychain), self.assertRaisesRegex(ls.LaunchSetError, "dedicated"):
                ls.sign(self.generation, identity="x", keychain=keychain, run=codesign)
        self.assertEqual([], codesign.calls)
        ls.sign(self.generation, identity="x", keychain="/k.keychain-db", run=codesign)
        argv = codesign.calls[0]
        self.assertEqual("/k.keychain-db", argv[argv.index("--keychain") + 1])
        self.assertEqual(ls.LAUNCH_SET_IDENTIFIER, argv[argv.index("--identifier") + 1])

    def test_a_keychain_that_is_not_ready_leaves_the_pointer_unchanged(self) -> None:
        older = self.written(make_generation(self.root, "b" * 40 + "-1"), sequence=1)
        self.publish(older)
        before = self.pointer.read_bytes()
        codesign = FakeCodesign()
        with self.assertRaisesRegex(ls.LaunchSetError, "not ready, pointer unchanged"):
            self.publish(self.written(), codesign, ready=lambda: "signing keychain is locked")
        self.assertEqual(before, self.pointer.read_bytes())
        self.assertEqual([], codesign.calls, "nothing signed")

    def test_a_failed_signature_leaves_the_pointer_unchanged(self) -> None:
        with self.assertRaisesRegex(ls.LaunchSetError, "signing failed"):
            self.publish(self.written(), FakeCodesign(sign_rc=1))
        self.assertFalse(self.pointer.exists())

    def test_publish_freezes_verifies_and_keeps_the_rollback_generation(self) -> None:
        older = self.written(make_generation(self.root, "b" * 40 + "-1"), sequence=1)
        self.assertEqual({"schema": 1, "generation": str(older), "previous": None}, self.publish(older))
        value = self.publish(self.written())
        self.assertEqual({"schema": 1, "generation": str(self.generation),
                          "previous": str(older)}, value)
        self.assertEqual(0o600, self.pointer.stat().st_mode & 0o777)
        for dirpath, dirs, files in os.walk(self.generation):
            for name in dirs + files:
                self.assertEqual(0, os.lstat(os.path.join(dirpath, name)).st_mode & 0o222, name)
        # Publishing the same generation again keeps the rollback target.
        self.assertEqual(str(older), self.publish(self.generation)["previous"])

    def test_contents_are_checked_against_every_digest(self) -> None:
        self.written()
        ls.freeze(self.generation)
        self.assertIsNone(ls.verify_contents(self.generation, self.root))
        for victim in ("tartci", "scripts/x.py", ls.ENTRYPOINT, ls.SHIM, ls.MANIFEST_NAME):
            with self.subTest(victim=victim):
                path = self.generation / victim
                original = path.read_bytes()
                os.chmod(path.parent, 0o755)
                os.chmod(path, 0o644)
                path.write_bytes(original + b"#")
                os.chmod(path, 0o444)
                self.assertIsNotNone(ls.verify_contents(self.generation, self.root))
                os.chmod(path, 0o644)
                path.write_bytes(original)
                os.chmod(path, 0o444)
                os.chmod(path.parent, 0o555)
        self.assertIsNone(ls.verify_contents(self.generation, self.root))

    def test_a_file_the_manifest_does_not_name_is_refused(self) -> None:
        self.written()
        ls.freeze(self.generation)
        cases = (("scripts/__pycache__/x.cpython-311.pyc", "bytecode cache"),
                 ("scripts/__pycache__/", "bytecode cache"),          # even empty
                 ("scripts/extra.py", "scripts/extra.py is not in the signed manifest"),
                 ("planted", "planted is not in the signed manifest"))
        for relative, why in cases:
            with self.subTest(relative=relative):
                target = self.generation / relative
                for directory in (self.generation, self.generation / "scripts"):
                    os.chmod(directory, 0o755)
                if relative.endswith("/"):
                    target.mkdir()
                else:
                    target.parent.mkdir(exist_ok=True)
                    target.write_bytes(b"\x00planted")
                self.assertIn(why, ls.verify_contents(self.generation, self.root) or "")
                if target.is_file():
                    target.unlink()
                if target.name == "__pycache__" or target.parent.name == "__pycache__":
                    (self.generation / "scripts/__pycache__").rmdir()
                ls.freeze(self.generation)
        self.assertIsNone(ls.verify_contents(self.generation, self.root))

    def test_a_python_start_up_variable_in_a_lane_is_refused(self) -> None:
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
            lanes = {"x": {"environment": {"TART_HOME": STORE, name: "/tmp"}}}
            with self.subTest(name=name), self.assertRaisesRegex(ls.LaunchSetError, name):
                ls.build(self.generation, lanes, tart_home=STORE, python=PIN, sequence=1)

    def test_a_fifo_in_the_generation_is_refused(self) -> None:
        self.written()
        os.mkfifo(self.generation / "scripts/pipe")
        ls.freeze(self.generation)
        self.assertEqual("scripts/pipe is not a regular file",
                         ls.verify_contents(self.generation, self.root))

    def test_an_unreadable_previous_generation_does_not_block_the_pointer(self) -> None:
        older = self.written(make_generation(self.root, "b" * 40 + "-1"), sequence=1)
        self.publish(older)
        os.chmod(older, 0o755)
        (older / ls.SET_NAME).unlink()      # damaged: its set is gone
        value = self.publish(self.written(sequence=2))
        self.assertEqual(str(self.generation), value["generation"])
        self.assertIsNone(value["previous"], "a damaged generation is not a rollback target")
        self.assertIn("treated as absent", value["note"])
        self.assertEqual(str(self.generation), ls.read_pointer(self.pointer)["generation"])

    def test_publish_refuses_a_sequence_that_does_not_advance(self) -> None:
        newer = self.written(make_generation(self.root, "c" * 40 + "-1"), sequence=5)
        self.publish(newer)
        before = self.pointer.read_bytes()
        for sequence in (5, 4):
            with self.subTest(sequence=sequence):
                with self.assertRaisesRegex(ls.LaunchSetError, "is not after 5, pointer unchanged"):
                    self.publish(self.written(sequence=sequence))
                self.assertEqual(before, self.pointer.read_bytes())

    def test_writable_or_symlinked_paths_are_untrusted(self) -> None:
        self.written()
        ls.freeze(self.generation)
        target = self.generation / "scripts/x.py"
        for path, mode in ((target, 0o464), (target, 0o446), (target.parent, 0o575),
                           (self.root, 0o775)):
            with self.subTest(path=path.name, mode=oct(mode)):
                before = path.stat().st_mode & 0o777
                os.chmod(path, mode)
                self.assertIn("writable", ls.trust_problem(target, self.root))
                os.chmod(path, before)
        self.assertIsNone(ls.trust_problem(target, self.root))
        os.chmod(self.generation, 0o755)
        link = self.generation / "link"
        link.symlink_to(target)
        self.assertIn("symlink", ls.trust_problem(link, self.root))
        self.assertIn("outside", ls.trust_problem(Path("/usr/bin/true"), self.root))

    def test_unsafe_member_paths_are_refused(self) -> None:
        self.written()
        manifest = json.loads((self.generation / ls.MANIFEST_NAME).read_text())
        manifest["members"].append({"path": "../escape", "sha256": "0" * 64})
        os.chmod(self.generation / ls.MANIFEST_NAME, 0o644)
        (self.generation / ls.MANIFEST_NAME).write_text(json.dumps(manifest))
        self.written()
        ls.freeze(self.generation)
        self.assertIn("unsafe member path", ls.verify_contents(self.generation, self.root))


if __name__ == "__main__":
    unittest.main()
