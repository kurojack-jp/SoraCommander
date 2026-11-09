"""Windows cooperating writer lock contention, disposable journal only."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from sora_commander_next.session_journal import write_snapshot,JournalError

@unittest.skipUnless(os.name=="nt","Windows byte-range lock")
class WriterLockTests(unittest.TestCase):
    def test_external_lock_fails_closed(self):
        import msvcrt
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"journal.json"
            lp=p.with_name(p.name+".lock")
            with open(lp,"a+b") as handle:
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                try:
                    with self.assertRaises(JournalError):
                        write_snapshot(p,[])
                    self.assertFalse(p.exists())
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            write_snapshot(p,[])
            self.assertTrue(p.exists())
if __name__=="__main__":
    unittest.main()
