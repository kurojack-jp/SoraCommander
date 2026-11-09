import pathlib,sys,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.release_anchor_review import review_anchor,AnchorReviewError
GOOD="a"*64
def args(**changes):
 d=dict(publisher_fingerprint=GOOD,owner_confirmed_fingerprint=GOOD,protected_floor=7,observed_floor=7,proposed_sequence=8)
 d.update(changes);return d
class TestAnchor(unittest.TestCase):
 def test_match_still_denies_all_authority(self):
  x=review_anchor(**args())
  self.assertFalse(x.deploy_allowed);self.assertFalse(x.remote_allowed)
  self.assertFalse(x.journal_write_allowed)
 def test_wrong_fingerprint(self):
  with self.assertRaises(AnchorReviewError):review_anchor(**args(owner_confirmed_fingerprint="b"*64))
 def test_unformatted_fingerprint(self):
  with self.assertRaises(AnchorReviewError):review_anchor(**args(owner_confirmed_fingerprint="bad"))
 def test_rollback(self):
  with self.assertRaises(AnchorReviewError):review_anchor(**args(observed_floor=6))
 def test_repeated_version(self):
  with self.assertRaises(AnchorReviewError):review_anchor(**args(proposed_sequence=7))
 def test_observed_higher_than_proposal(self):
  with self.assertRaises(AnchorReviewError):review_anchor(**args(observed_floor=9))
 def test_counter_boolean_rejected(self):
  with self.assertRaises(AnchorReviewError):review_anchor(**args(proposed_sequence=True))
if __name__=="__main__":unittest.main()
