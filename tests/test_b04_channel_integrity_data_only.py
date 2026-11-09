"""DEV_ONLY HMAC integrity fixture: NEVER OS identity or permission evidence."""
import hashlib
import hmac
import unittest
from dataclasses import replace
from offline_b04_channel_evidence_model import Expected, ClaimedMessage, inspect_data_only

KEY = b"offline-synthetic-key-not-a-service-secret"
OTHER_KEY = b"unrelated-untrusted-test-key"

def transcript(expected, message, payload):
    # This encodes synthetic fixture context, not the production wire format.
    chunks = (expected.installation, expected.generation, expected.owner,
              message.epoch, message.direction, str(message.sequence))
    return b"b04-offline-fixture-v1\x00" + b"\x00".join(
        x.encode("ascii") for x in chunks
    ) + len(payload).to_bytes(4, "big") + payload

def tag(key, expected, message, payload):
    return hmac.digest(key, transcript(expected, message, payload), "sha256")

class IntegrityDataOnlyTests(unittest.TestCase):
    def setUp(self):
        self.expected = Expected("1"*32, "2"*32, "S-1-5-21-42", "3"*32,
                                 "SERVICE_TO_CLIENT", 5)
        self.msg = ClaimedMessage("1"*32, "2"*32, "S-1-5-21-42",
                                  "3"*32, "SERVICE_TO_CLIENT", 5)
        self.payload = b"synthetic-data"
        self.signature = tag(KEY, self.expected, self.msg, self.payload)

    def check(self, msg=None, payload=None, key=KEY, signature=None):
        msg = self.msg if msg is None else msg
        payload = self.payload if payload is None else payload
        signature = self.signature if signature is None else signature
        intact = hmac.compare_digest(tag(key, self.expected, msg, payload), signature)
        matching, authority, admission = inspect_data_only(self.expected, msg)
        self.assertEqual(authority, "NOT_AUTHENTICATED")
        self.assertEqual(admission, "UNAVAILABLE")
        return intact and matching

    def test_valid_synthetic_tag_still_not_authenticated(self):
        self.assertTrue(self.check())

    def test_payload_tampering(self):
        self.assertFalse(self.check(payload=b"tampered"))

    def test_wrong_key(self):
        self.assertFalse(self.check(key=OTHER_KEY))

    def test_cross_epoch_relay(self):
        self.assertFalse(self.check(msg=replace(self.msg, epoch="4"*32)))

    def test_direction_reflection(self):
        self.assertFalse(self.check(msg=replace(self.msg, direction="CLIENT_TO_SERVICE")))

    def test_sequence_replay(self):
        self.assertFalse(self.check(msg=replace(self.msg, sequence=4)))

    def test_other_owner(self):
        self.assertFalse(self.check(msg=replace(self.msg, owner="S-1-5-21-99")))

    def test_untrusted_key_self_signing_does_not_grant(self):
        forged = tag(OTHER_KEY, self.expected, self.msg, self.payload)
        self.assertFalse(self.check(key=OTHER_KEY))
        self.assertFalse(hmac.compare_digest(self.signature, forged))
        self.assertEqual(inspect_data_only(self.expected, self.msg),
                         (True, "NOT_AUTHENTICATED", "UNAVAILABLE"))

    def test_rebound_expectations_cannot_prove_identity(self):
        changed = replace(self.expected, epoch="4"*32)
        msg = replace(self.msg, epoch="4"*32)
        self.assertTrue(hmac.compare_digest(tag(KEY, changed, msg, self.payload),
                                            tag(KEY, changed, msg, self.payload)))
        self.assertEqual(inspect_data_only(changed, msg),
                         (True, "NOT_AUTHENTICATED", "UNAVAILABLE"))
