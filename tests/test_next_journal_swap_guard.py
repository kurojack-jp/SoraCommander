"""Reject a journal target substituted during staging; disposable files only."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from sora_commander_next.session_journal import write_snapshot,JournalError

class SwapGuardTests(unittest.TestCase):
    def test_target_appears_during_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/"journal.json"
            import os
            original=os.fsync
            def concurrent_change(fd):
                original(fd)
                p.write_text("OTHER PROCESS",encoding="utf-8")
            with patch("sora_commander_next.session_journal.os.fsync",side_effect=concurrent_change):
                with self.assertRaises(JournalError):
                    write_snapshot(p,[])
            self.assertEqual(p.read_text(),"OTHER PROCESS")
            self.assertFalse(list(Path(temp).glob(".sora-session-*.tmp")))
    def test_target_replaced_during_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/"journal.json"
            p.write_text("ORIGINAL")
            import os
            original=os.fsync
            def concurrent_change(fd):
                original(fd)
                p.unlink()
                p.write_text("OTHER PROCESS")
            with patch("sora_commander_next.session_journal.os.fsync",side_effect=concurrent_change):
                with self.assertRaises(JournalError):
                    write_snapshot(p,[])
            self.assertEqual(p.read_text(),"OTHER PROCESS")

if __name__=="__main__":
    unittest.main()
