"""Opt-in JobManager journal integration, including terminal state."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from sora_commander_next.commands import Command
from sora_commander_next.jobs import JobManager
from sora_commander_next.session_journal import read_previous_snapshot

ROOT=Path(__file__).resolve().parents[1]

class JournalIntegrationTests(unittest.TestCase):
    def test_start_and_finish_persist_metadata(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"sessions.json"
            jobs=JobManager(journal_path=p)
            try:
                cmd=Command(sys.executable,("-c","import time;time.sleep(.3);print('DONE')"),str(ROOT),timeout=3)
                sid=jobs.start(cmd)["session_id"]
                initial=read_previous_snapshot(p)
                self.assertEqual(initial["count"],1)
                self.assertEqual(initial["sessions"][0]["session_id"],sid)
                self.assertEqual(initial["sessions"][0]["status"],"INTERRUPTED_UNKNOWN")
                completed=jobs.wait(sid)
                self.assertEqual(completed["status"],"COMPLETED")
                recorded=read_previous_snapshot(p)
                self.assertEqual(recorded["sessions"][0]["status"],"COMPLETED")
                self.assertEqual(recorded["sessions"][0]["exit_code"],0)
                self.assertNotIn("DONE",p.read_text())
            finally:
                jobs.close()
