"""Characterize the last check-to-replace gap; no production file access."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from sora_commander_next.session_journal import write_snapshot

class FinalReplaceGap(unittest.TestCase):
    def test_concurrent_swap_after_last_check_is_observable_risk(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/"journal.json"
            target.write_text("OLD",encoding="utf-8")
            original=os.replace
            changed=[]
            def intervene(source,destination):
                destination=Path(destination)
                destination.unlink()
                destination.write_text("CONCURRENT-WRITER",encoding="utf-8")
                changed.append(True)
                return original(source,destination)
            with patch("sora_commander_next.session_journal.os.replace",side_effect=intervene):
                write_snapshot(target,[])
            self.assertTrue(changed)
            self.assertNotEqual(target.read_text(encoding="utf-8"),"CONCURRENT-WRITER")
            # This is a characterization test, not proof of safety.
            # Code currently overwrites a competing modification at the final boundary.
if __name__=="__main__":
    unittest.main()
