import unittest
from dataclasses import replace
from sora_commander_next.offline_b04_pipe_binding_candidate import (
    PipeBindingObservation, PipeBindingDenied, review_pipe_binding
)

class PipeBindingCandidateTests(unittest.TestCase):
    def setUp(self):
        self.base = PipeBindingObservation("handle-A","handle-A",7,7,2,2,
            "a"*64,"a"*64,"b"*64,"b"*64)
    def test_match_is_never_authentication(self):
        result = review_pipe_binding(self.base)
        self.assertTrue(result.observations_match)
        self.assertEqual(result.authentication,"NOT_AUTHENTICATED")
        self.assertEqual(result.admission,"UNAVAILABLE")
    def test_mismatch_rejected_as_evidence(self):
        for field,value in (
            ("transcript_handle_identity","handle-B"),
            ("transcript_epoch",8),
            ("transcript_owner_session_id",3),
            ("transcript_client_nonce","c"*64),
            ("transcript_server_nonce","d"*64),
            ("server_nonce","a"*64),
        ):
            with self.subTest(field=field):
                self.assertFalse(review_pipe_binding(replace(self.base,**{field:value})).observations_match)
    def test_bad_types_denied(self):
        for field,value in (("connection_epoch",True),("owner_session_id",-1),
                ("client_nonce","z"*64),("connected_handle_identity","")):
            with self.subTest(field=field):
                with self.assertRaises(PipeBindingDenied):
                    replace(self.base,**{field:value})
    def test_wrong_observation_denied(self):
        with self.assertRaises(PipeBindingDenied):
            review_pipe_binding({})
