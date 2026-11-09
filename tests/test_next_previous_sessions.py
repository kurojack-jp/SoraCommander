"""Prior-run metadata is observable but never replayed or controllable."""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from sora_commander_next.jobs import JobManager
from sora_commander_next.host import Host
from sora_commander_next.session_journal import write_snapshot, JournalError

class PreviousSessionsTests(unittest.TestCase):
    def test_previous_running_is_unknown_and_not_controllable(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"journal.json"
            sid="b"*48
            write_snapshot(p,[dict(session_id=sid,status="RUNNING",pid=1234,exit_code=None,captured_bytes=0,output_incomplete=False)])
            jobs=JobManager(journal_path=p)
            try:
                h=Host(jobs=jobs)
                self.assertEqual(h.previous_sessions()["sessions"][0]["status"],"INTERRUPTED_UNKNOWN")
                self.assertEqual(h.list_sessions()["count"],0)
                self.assertEqual(h.stop_process(sid)["status"],"ERROR")
                self.assertEqual(h.read_process_output(sid)["status"],"ERROR")
                h.previous_sessions()["sessions"][0]["status"]="COMPLETED"
                self.assertEqual(h.previous_sessions()["sessions"][0]["status"],"INTERRUPTED_UNKNOWN")
            finally: jobs.close()
    def test_bad_prior_journal_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"journal.json"
            p.write_text('garbage',encoding="utf-8")
            with self.assertRaises(JournalError):
                JobManager(journal_path=p)
    def test_default_no_history(self):
        jobs=JobManager()
        try: self.assertEqual(jobs.previous_sessions(),{"sessions":[],"count":0})
        finally: jobs.close()
