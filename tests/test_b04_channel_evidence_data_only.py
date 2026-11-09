"""Synthetic B04 negative tests: no crypto, native IPC, service or execution."""
from dataclasses import replace
import unittest
from offline_b04_channel_evidence_model import Expected, ClaimedMessage, EvidenceError, inspect_data_only

class ChannelEvidenceDataOnlyTests(unittest.TestCase):
    def setUp(self):
        self.expected = Expected("1"*32, "2"*32, "S-1-5-21-42", "3"*32,
                                 "SERVICE_TO_CLIENT", 5)
        self.message = ClaimedMessage("1"*32, "2"*32, "S-1-5-21-42",
                                      "3"*32, "SERVICE_TO_CLIENT", 5)

    def check(self, message, matched):
        self.assertEqual(inspect_data_only(self.expected, message),
                         (matched, "NOT_AUTHENTICATED", "UNAVAILABLE"))

    def test_equal_data_still_untrusted(self):
        self.check(self.message, True)

    def test_claimed_authentication_cannot_promote(self):
        self.check(replace(self.message, claimed_authenticated=True), True)

    def test_cross_epoch(self):
        self.check(replace(self.message, epoch="4"*32), False)

    def test_cross_owner(self):
        self.check(replace(self.message, owner="S-1-5-21-99"), False)

    def test_cross_generation(self):
        self.check(replace(self.message, generation="5"*32), False)

    def test_cross_installation(self):
        self.check(replace(self.message, installation="6"*32), False)

    def test_reflection(self):
        self.check(replace(self.message, direction="CLIENT_TO_SERVICE"), False)

    def test_replay_old_sequence(self):
        self.check(replace(self.message, sequence=4), False)

    def test_skip_sequence(self):
        self.check(replace(self.message, sequence=6), False)

    def test_bad_sequence_types_fail_closed(self):
        for seq in (True, "5", -1, 2**63, None, 5.0):
            with self.subTest(seq=seq), self.assertRaises(EvidenceError):
                inspect_data_only(self.expected, replace(self.message, sequence=seq))

    def test_wrong_struct_types_fail_closed(self):
        for obj in ({"sequence": 5}, None):
            with self.subTest(obj=obj), self.assertRaises(EvidenceError):
                inspect_data_only(self.expected, obj)

    def test_forged_expected_state_also_never_grants(self):
        self.assertEqual(inspect_data_only(replace(self.expected, epoch="4"*32),
                                           replace(self.message, epoch="4"*32)),
                         (True, "NOT_AUTHENTICATED", "UNAVAILABLE"))

    def test_invalid_expected_identifiers(self):
        for change in ({"epoch":"G"*32}, {"next_sequence":True},
                       {"owner":""}, {"direction":"BOTH"}):
            with self.subTest(change=change), self.assertRaises(EvidenceError):
                Expected(**{**{k:getattr(self.expected,k) for k in
                    ("installation","generation","owner","epoch","direction","next_sequence")}, **change})
