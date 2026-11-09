"""B04 DEV_ONLY hostile transcript integration: never authorize from observations."""
import unittest
from dataclasses import replace
from sora_commander_next.offline_b04_composite_review_candidate import review_composite
from sora_commander_next.offline_b04_pipe_binding_candidate import PipeBindingObservation
from sora_commander_next.offline_b04_trust_evidence_candidate import B04TrustEvidence
from sora_commander_next.offline_b04_message_integrity_candidate import (
    OfflineSequenceReview, MessageEnvelope, demo_mac,
)
KEY=bytes(range(32))
A="a"*64
B="b"*64

class ChannelAbuseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.bind=PipeBindingObservation("same-handle","same-handle",7,7,2,2,A,A,B,B)
        self.trust=B04TrustEvidence("a"*64,"b"*64,"c"*64,"d"*64,"1"*32,1,7,7,True,True,True)
        self.tracker=OfflineSequenceReview(epoch=7,client_nonce=A,server_nonce=B)

    def check(self, seq, *, bind=None, trust=None, epoch=7, direction="client-to-service", payload=b"synthetic", key=KEY):
        env=MessageEnvelope(epoch,direction,seq,A,B,payload)
        r=review_composite(bind or self.bind,trust or self.trust,self.tracker,env,KEY,demo_mac(key,env))
        self.assertEqual((r.authentication,r.admission),("NOT_AUTHENTICATED","UNAVAILABLE"))
        return r

    def test_bidirectional_sequence_and_duplicate(self):
        for direction in ("client-to-service","service-to-client"):
            for seq in (1,2,3):
                self.assertTrue(self.check(seq,direction=direction).sequence_next)
                self.assertFalse(self.check(seq,direction=direction).sequence_next)

    def test_wrong_pipe_and_trust_cannot_advance(self):
        self.assertFalse(self.check(1,bind=replace(self.bind,transcript_handle_identity="other")).sequence_next)
        self.assertFalse(self.check(1,trust=replace(self.trust,protected_anchor_observed=False)).sequence_next)
        self.assertTrue(self.check(1).sequence_next)

    def test_relay_old_epoch_rejected(self):
        self.assertFalse(self.check(1,epoch=8).sequence_next)
        self.assertTrue(self.check(1).sequence_next)

    def test_bad_mac_does_not_consume(self):
        self.assertFalse(self.check(1,key=b"z"*32).sequence_next)
        self.assertTrue(self.check(1).sequence_next)

    def test_reject_sequence_gap_and_recovery(self):
        self.assertTrue(self.check(1).sequence_next)
        self.assertFalse(self.check(3).sequence_next)
        self.assertTrue(self.check(2).sequence_next)
        self.assertTrue(self.check(3).sequence_next)

    def test_expected_observation_can_be_forged_without_authority(self):
        # This proves the acceptance gate is still missing, not authentication.
        r=self.check(1)
        self.assertTrue(r.binding_observations_match)
        self.assertTrue(r.trust_observations_complete)
        self.assertTrue(r.mac_matches)
        self.assertFalse(hasattr(r,"execute"))
