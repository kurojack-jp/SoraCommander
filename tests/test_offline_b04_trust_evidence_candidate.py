import unittest
from dataclasses import replace
from sora_commander_next.offline_b04_trust_evidence_candidate import (
 B04TrustEvidence,TrustReviewDenied,review_untrusted_evidence
)

class TrustEvidenceTests(unittest.TestCase):
 def setUp(self):
  self.item=B04TrustEvidence("a"*64,"b"*64,"c"*64,"d"*64,
   "1"*32,1,7,7,True,True,True)
 def test_even_full_attestations_never_authorize(self):
  r=review_untrusted_evidence(self.item)
  self.assertTrue(r.evidence_complete)
  self.assertEqual(r.authentication,"NOT_AUTHENTICATED")
  self.assertEqual(r.admission,"UNAVAILABLE")
 def test_pipe_epoch_mismatch_detected_without_admission(self):
  r=review_untrusted_evidence(replace(self.item,observer_pipe_epoch=8))
  self.assertFalse(r.pipe_epoch_consistent)
  self.assertEqual(r.admission,"UNAVAILABLE")
 def test_missing_independent_trust_observations(self):
  for field in ("trusted_installer_origin","protected_anchor_observed","native_peer_verified"):
   with self.subTest(field=field):
    r=review_untrusted_evidence(replace(self.item,**{field:False}))
    self.assertFalse(r.evidence_complete)
 def test_invalid_evidence_rejected(self):
  for field,val in (("publisher_spki_sha256","A"*64),("installer_sha256","x"),
    ("installation_id",""),("generation",0),("connected_pipe_epoch",True),
    ("native_peer_verified",1)):
   with self.subTest(field=field):
    with self.assertRaises(TrustReviewDenied):
     replace(self.item,**{field:val})
 def test_self_reported_trust_cannot_become_permission(self):
  r=review_untrusted_evidence(self.item)
  self.assertFalse(hasattr(r,"authorized"))
  self.assertFalse(hasattr(r,"commit_allowed"))
