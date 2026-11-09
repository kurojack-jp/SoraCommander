import json,pathlib,sys,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.release_journal_recovery import parse_record,review_recovery,JournalRecoveryError
def record(seq,high,phase="STABLE"):
 return json.dumps({"sequence":seq,"high_water":high,"phase":phase}).encode("ascii")
class TestJournal(unittest.TestCase):
 def test_invalid_phase_types_rejected(self):
  for phase in ([],{},None,False,True,0,1,1.5):
   with self.subTest(phase=phase):
    with self.assertRaises(JournalRecoveryError):
     parse_record(record(3,3,phase))
 def test_valid_phases_remain_manual_review_only(self):
  for phase in ("STABLE","PREPARED"):
   with self.subTest(phase=phase):
    raw=record(3,3,phase)
    self.assertEqual(parse_record(raw),{"sequence":3,"high_water":3,"phase":phase})
    r=review_recovery(main_bytes=raw,external_protected_floor=3)
    self.assertEqual(r["status"],"MANUAL_REVIEW_ONLY")
    for flag in ("automatic_recovery","writes_allowed","install_allowed","remote_allowed"):
     self.assertIs(r[flag],False)
 def test_stable_is_not_authorizing(self):
  r=review_recovery(main_bytes=record(3,3),external_protected_floor=3)
  self.assertFalse(r["writes_allowed"]);self.assertFalse(r["automatic_recovery"])
 def test_pending_requires_manual_review(self):
  r=review_recovery(main_bytes=record(3,3),pending_bytes=record(3,4,"PREPARED"),external_protected_floor=3)
  self.assertTrue(r["pending_found"]);self.assertEqual(r["observed_high_water"],4)
 def test_old_main_rejected(self):
  with self.assertRaises(JournalRecoveryError):
   review_recovery(main_bytes=record(2,2),external_protected_floor=3)
 def test_old_pending_rejected(self):
  with self.assertRaises(JournalRecoveryError):
   review_recovery(main_bytes=record(3,5),pending_bytes=record(2,4),external_protected_floor=3)
 def test_corrupt_rejected(self):
  with self.assertRaises(JournalRecoveryError):
   review_recovery(main_bytes=b"{",external_protected_floor=0)
 def test_duplicate_keys_rejected(self):
  with self.assertRaises(JournalRecoveryError):
   review_recovery(main_bytes=b'{"sequence":1,"sequence":2,"high_water":2,"phase":"STABLE"}',external_protected_floor=0)
 def test_bool_floor_rejected(self):
  with self.assertRaises(JournalRecoveryError):
   review_recovery(main_bytes=record(1,1),external_protected_floor=True)
if __name__=="__main__":unittest.main()
