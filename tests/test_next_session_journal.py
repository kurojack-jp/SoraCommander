"""Restart journal only reports metadata; it must never replay a prior job."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sora_commander_next.session_journal import write_snapshot, read_previous_snapshot, JournalError

def entry(status="RUNNING"):
    return {"session_id":"a"*48,"status":status,"pid":123,
            "exit_code":None if status=="RUNNING" else 0,
            "captured_bytes":42,"output_incomplete":False}

class JournalTests(unittest.TestCase):
    def test_running_is_unknown_after_restart(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"journal.json"
            write_snapshot(p,[entry()])
            out=read_previous_snapshot(p)
            self.assertEqual(out["sessions"][0]["status"],"INTERRUPTED_UNKNOWN")
            self.assertIsNone(out["sessions"][0]["exit_code"])
            self.assertEqual(json.loads(p.read_text())["sessions"][0]["status"],"RUNNING")
    def test_terminal_preserved_and_replaced(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"journal.json"
            write_snapshot(p,[entry("COMPLETED")])
            self.assertEqual(read_previous_snapshot(p)["sessions"][0]["status"],"COMPLETED")
            write_snapshot(p,[])
            self.assertEqual(read_previous_snapshot(p),{"sessions":[],"count":0})
    def test_malformed_and_extra_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"journal.json"
            with self.assertRaises(JournalError):
                write_snapshot(p,[dict(entry(),argv=["SECRET"])])
            p.write_text('{"version":1,"sessions":[{"bad":"data"}]}')
            with self.assertRaises(JournalError):
                read_previous_snapshot(p)
    def test_no_file_created_on_bad_list(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"journal.json"
            with self.assertRaises(JournalError):
                write_snapshot(p,[entry()]*257)
            self.assertFalse(p.exists())

if __name__=="__main__":
    unittest.main()
