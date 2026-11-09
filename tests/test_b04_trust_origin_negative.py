"""DEV_ONLY trust-origin negative checks; no protected installer or native keys."""
import hmac
import unittest
from dataclasses import replace
from offline_b04_channel_evidence_model import Expected, ClaimedMessage, inspect_data_only
from test_b04_channel_integrity_data_only import tag, KEY, OTHER_KEY

class TrustOriginNegativeTests(unittest.TestCase):
    def setUp(self):
        self.expected = Expected("1"*32, "2"*32, "S-1-5-21-42", "3"*32,
                                 "SERVICE_TO_CLIENT", 5)
        self.message = ClaimedMessage("1"*32, "2"*32, "S-1-5-21-42",
                                      "3"*32, "SERVICE_TO_CLIENT", 5)
        self.payload = b"simulated response"

    def never_authorizes(self, expected, message):
        matched, authority, admission = inspect_data_only(expected, message)
        self.assertEqual(authority, "NOT_AUTHENTICATED")
        self.assertEqual(admission, "UNAVAILABLE")
        return matched

    def test_known_key_authentic_tag_is_still_not_trust(self):
        proof = tag(KEY, self.expected, self.message, self.payload)
        self.assertTrue(hmac.compare_digest(proof,
            tag(KEY, self.expected, self.message, self.payload)))
        self.assertTrue(self.never_authorizes(self.expected, self.message))

    def test_attacker_self_selected_key_is_not_pin(self):
        proof = tag(OTHER_KEY, self.expected, self.message, self.payload)
        self.assertTrue(hmac.compare_digest(proof,
            tag(OTHER_KEY, self.expected, self.message, self.payload)))
        self.assertFalse(hmac.compare_digest(proof,
            tag(KEY, self.expected, self.message, self.payload)))
        self.never_authorizes(self.expected, self.message)

    def test_forged_matching_context_also_has_no_authority(self):
        changed = replace(self.expected, generation="a"*32, epoch="b"*32)
        claimed = replace(self.message, generation="a"*32, epoch="b"*32)
        self.assertTrue(self.never_authorizes(changed, claimed))

    def test_cross_epoch_relay_cannot_match_frozen_expectation(self):
        self.assertFalse(self.never_authorizes(self.expected,
            replace(self.message, epoch="4"*32)))

    def test_cross_owner_relay_cannot_match_frozen_expectation(self):
        self.assertFalse(self.never_authorizes(self.expected,
            replace(self.message, owner="S-1-5-21-99")))

    def test_equal_context_with_claimed_auth_still_unavailable(self):
        self.assertTrue(self.never_authorizes(self.expected,
            replace(self.message, claimed_authenticated=True)))
