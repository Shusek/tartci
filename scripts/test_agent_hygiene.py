import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "agent-hygiene"


class AgentHygieneTests(unittest.TestCase):
    def run_script(self, *args, snapshot, transcripts, extra=None):
        env = os.environ.copy()
        env.update({
            "AH_PS_SNAPSHOT_FILE": str(snapshot),
            "AH_TRANSCRIPT_DIRS": str(transcripts),
            "AH_STALE_DAYS": "2",
            "AH_PROC_PCT": "1000",
            "AH_SWAP_PCT": "1000",
        })
        if extra:
            env.update(extra)
        return subprocess.run([str(SCRIPT), *args], text=True, capture_output=True, env=env)

    def test_report_uses_transcript_activity_and_real_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot = root / "ps"
            snapshot.write_text("100 1 00-00:01 10 /Users/test/.local/bin/claude\n")
            transcripts = root / "transcripts"
            transcripts.mkdir()
            transcript = transcripts / "session-100.jsonl"
            transcript.write_text("recent")
            old = time.time() - 3 * 86400
            os.utime(transcript, (old, old))
            result = self.run_script("--report", snapshot=snapshot, transcripts=transcripts)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("100 3d", result.stdout)

    def test_reap_refuses_agent_context_and_does_not_use_process_etime(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot = root / "ps"
            snapshot.write_text("100 1 00-00:01 10 /Users/test/.local/bin/claude\n")
            transcripts = root / "transcripts"
            transcripts.mkdir()
            transcript = transcripts / "session-100.jsonl"
            transcript.write_text("recent")
            old = time.time() - 3 * 86400
            os.utime(transcript, (old, old))
            result = self.run_script("--reap", "2", "--yes", snapshot=snapshot,
                                     transcripts=transcripts, extra={"CODEX_SANDBOX": "1"})
            self.assertEqual(result.returncode, 2)
            self.assertIn("human-only", result.stderr)
            self.assertIn("--yes is ignored", result.stderr) if "--yes is ignored" in result.stderr else None

    def test_live_subagent_child_is_protected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot = root / "ps"
            snapshot.write_text("100 1 00-00:01 10 /Users/test/.local/bin/claude\n101 100 00-00:01 10 subagent --resume\n")
            transcripts = root / "transcripts"
            transcripts.mkdir()
            transcript = transcripts / "session-100.jsonl"
            transcript.write_text("old")
            old = time.time() - 3 * 86400
            os.utime(transcript, (old, old))
            result = self.run_script("--reap", "2", snapshot=snapshot,
                                     transcripts=transcripts, extra={"CODEX_SANDBOX": "1"})
            self.assertEqual(result.returncode, 0)
            self.assertIn("sessions older than 2d: 0", result.stdout)


if __name__ == "__main__":
    unittest.main()
