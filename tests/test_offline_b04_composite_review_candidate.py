import unittest
from dataclasses import replace
from sora_commander_next.offline_b04_pipe_binding_candidate import PipeBindingObservation
from sora_commander_next.offline_b04_trust_evidence_candidate import B04TrustEvidence
from sora_commander_next.offline_b04_message_integrity_candidate import OfflineSequenceReview,MessageEnvelope,demo_mac
from sora_commander_next.offline_b04_composite_review_candidate import review_composite
A="a"*64
B="b"*64
K=bytes(range(32))
class CompositeTests(unittest.TestCase):
 def setUp(self):
  self.b=PipeBindingObservation("handle","handle",7,7,2,2,A,A,B,B)
  self.t=B04TrustEvidence("a"*64,"b"*64,"c"*64,"d"*64,"1"*32,1,7,7,True,True,True)
  self.e=MessageEnvelope(7,"client-to-service",1,A,B,b"fixture")
  self.r=OfflineSequenceReview(epoch=7,client_nonce=A,server_nonce=B)
 def check(self,b=None,t=None,e=None,mac=None):
  e=e or self.e
  return review_composite(b or self.b,t or self.t,self.r,e,K,mac if mac is not None else demo_mac(K,e))
 def test_perfect_observations_never_authorize(self):
  v=self.check()
  self.assertTrue(v.binding_observations_match and v.trust_observations_complete and v.mac_matches and v.sequence_next)
  self.assertEqual((v.authentication,v.admission),("NOT_AUTHENTICATED","UNAVAILABLE"))
 def test_epoch_and_nonce_conflicts(self):
  for field,val in (("connection_epoch",8),("client_nonce","c"*64),("connected_handle_identity","different")):
   with self.subTest(field=field):
    self.assertFalse(self.check(b=replace(self.b,**{field:val})).sequence_next)
  self.assertTrue(self.check().sequence_next) # mismatches must not consume counter
 def test_untrusted_installer_and_peer(self):
  for field in ("trusted_installer_origin","protected_anchor_observed","native_peer_verified"):
   with self.subTest(field=field):
    self.assertFalse(self.check(t=replace(self.t,**{field:False})).sequence_next)
  self.assertTrue(self.check().sequence_next)
 def test_bad_mac_does_not_consume_counter(self):
  self.assertFalse(self.check(mac=b"0"*32).sequence_next)
  self.assertTrue(self.check().sequence_next)
  self.assertFalse(self.check().sequence_next)
 def test_mismatched_message_epoch(self):
  e=replace(self.e,epoch=8)
  self.assertFalse(self.check(e=e).sequence_next)
  self.assertTrue(self.check().sequence_next)
