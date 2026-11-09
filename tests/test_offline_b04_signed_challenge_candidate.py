"""In-memory P-256 ECDSA proof math for B04 standalone PC candidate.

All test keys are synthetic, generated in RAM; none written, installed, or
passed to Windows CNG. Never touches real service, pipe, Key Storage Provider,
Owner approval, protected DB, network or job execution.
"""
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import patch
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, utils

from sora_commander_next.offline_b04_signed_challenge_candidate import (
    DOMAIN, FORMAT, P256_ORDER,
    SignedChallengeExpectation, SignedChallengeDenied,
    verify_signed_service_challenge_data_only, canonical_payload_bytes,
)

PINS = SignedChallengeExpectation(
    installation_id="a"*32, generation="b"*32,
    broker_instance_id="c"*32, client_nonce="d"*64,
    service_nonce="e"*64, connection_epoch=41, owner_sid="S-1-5-21-100-200-300",
    owner_logon_luid=(0x12345678, 0x90abcdef),
    owner_session_id=5, service_sid="S-1-5-80-100-200-300-400-500",
    installed_release_sha256="f"*64, approved_service_image_sha256="1"*64,
    command_snapshot_sha256="2"*64, deadline_tick_ms=100000,
)


def sign_low_s(private, data):
    raw=private.sign(DOMAIN+data, ec.ECDSA(hashes.SHA256()))
    r,s=utils.decode_dss_signature(raw)
    return utils.encode_dss_signature(r,min(s,P256_ORDER-s))


class SignedChallengeUnitTests(unittest.TestCase):
    def setUp(self):
        for symbol in ("ctypes.WinDLL","subprocess.Popen","os.system",
                       "socket.socket","sqlite3.connect"):
            guard=patch(symbol,side_effect=AssertionError("NATIVE/IPC/DB/SPAWN FORBIDDEN"),create=True)
            guard.start()
            self.addCleanup(guard.stop)
        self.key=ec.generate_private_key(ec.SECP256R1())  # synthetic in RAM
        self.spki=self.key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        self.pin=sha256(self.spki).hexdigest()
        self.expected=PINS
        self.payload=canonical_payload_bytes(self.expected)
        self.signature=sign_low_s(self.key,self.payload)

    def evaluate(self, **kwargs):
        args=dict(expected=self.expected,payload=self.payload,
                  signature_der=self.signature,
                  public_spki_der=self.spki,
                  independently_pinned_spki_sha256=self.pin,
                  _clock_ms=lambda:999)
        args.update(kwargs)
        return verify_signed_service_challenge_data_only(**args)

    def deny(self,code, **kwargs):
        with self.assertRaises(SignedChallengeDenied) as outcome:
            self.evaluate(**kwargs)
        self.assertEqual(outcome.exception.code,code)

    def test_valid_real_math_is_still_not_authenticated(self):
        r=self.evaluate()
        self.assertTrue(r.mathematical_signature_verified)
        self.assertEqual(r.authentication,"NOT_AUTHENTICATED")
        self.assertEqual(r.admission,"UNAVAILABLE")
        self.assertEqual(r.verification_algorithm,"ECDSA_P256_SHA256_DER_LOW_S")
        for flag in ("independent_installer_key_anchor_verified",
                     "service_private_key_nonexportable_os_verified",
                     "private_key_acl_and_service_sid_verified",
                     "connected_named_pipe_transcript_bound",
                     "sign_operation_was_by_live_service_proven",
                     "challenge_nonce_rng_os_verified",
                     "publisher_and_release_generation_os_verified",
                     "native_cng_signature_abi_verified"):
            self.assertFalse(getattr(r,flag))

    def test_actual_cryptographic_signing_is_not_simulated(self):
        self.assertEqual(self.evaluate().signature_sha256,
                         sha256(self.signature).hexdigest())
        with self.assertRaises(FrozenInstanceError):
            self.evaluate().authentication="AUTHENTICATED"

    def test_expected_payload_has_exact_canonical_format(self):
        raw=canonical_payload_bytes(self.expected)
        parsed=json.loads(raw)
        self.assertEqual(parsed["format"], FORMAT)
        self.assertEqual(parsed["connection_epoch"],41)
        self.assertEqual(parsed["service_sid"],"S-1-5-80-100-200-300-400-500")
        self.assertEqual(raw,json.dumps(parsed,sort_keys=True,
             ensure_ascii=True,separators=(",",":")).encode("ascii"))
        self.assertEqual(self.evaluate().payload_sha256,sha256(raw).hexdigest())

    def test_signature_invalid_for_different_payload_byte(self):
        data=bytearray(self.payload)
        data[-3] = ord("8")
        self.deny("CHALLENGE_CONTEXT_OR_CANONICAL_MISMATCH",payload=bytes(data))

    def test_signature_modified_byte_is_denied(self):
        bits=bytearray(self.signature); bits[-1] ^= 1
        with self.assertRaises(SignedChallengeDenied):
            self.evaluate(signature_der=bytes(bits))

    def test_correct_payload_signed_by_foreign_key_denied(self):
        attacker=ec.generate_private_key(ec.SECP256R1())
        forged=sign_low_s(attacker,self.payload)
        self.deny("ECDSA_SIGNATURE_INVALID",signature_der=forged)

    def test_independent_pinned_spki_hash_exact(self):
        self.deny("PUBLIC_KEY_NOT_PINNED",
                  independently_pinned_spki_sha256="0"*64)

    def test_pinned_key_cannot_be_replaced_by_attacker(self):
        attacker=ec.generate_private_key(ec.SECP256R1())
        foreign=attacker.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        self.deny("PUBLIC_KEY_NOT_PINNED",public_spki_der=foreign)

    def test_publisher_trust_not_asserted_by_exact_pin(self):
        r=self.evaluate()
        self.assertFalse(r.independent_installer_key_anchor_verified)
        self.assertIn("INDEPENDENT_INSTALLER_PUBLIC_KEY_ANCHOR_NOT_ACCEPTED",
                      r.missing_evidence)

    def test_expired_challenge_denied(self):
        self.deny("EXPIRED_OR_INVALID_CHALLENGE_CLOCK",_clock_ms=lambda:100000)

    def test_bad_clock_type_denied(self):
        for value in (None,True,1.2,-1,"50"):
            with self.subTest(value=value):
                self.deny("EXPIRED_OR_INVALID_CHALLENGE_CLOCK",
                          _clock_ms=lambda:value)

    def test_signed_generation_is_pinned(self):
        forged=replace(self.expected,generation="3"*32)
        self.deny("CHALLENGE_CONTEXT_OR_CANONICAL_MISMATCH",expected=forged)

    def test_signed_client_nonce_is_pinned(self):
        forged=replace(self.expected,client_nonce="9"*64)
        self.deny("CHALLENGE_CONTEXT_OR_CANONICAL_MISMATCH",expected=forged)

    def test_signed_service_nonce_is_pinned(self):
        forged=replace(self.expected,service_nonce="9"*64)
        self.deny("CHALLENGE_CONTEXT_OR_CANONICAL_MISMATCH",expected=forged)

    def test_signed_connection_epoch_is_pinned(self):
        forged=replace(self.expected,connection_epoch=42)
        self.deny("CHALLENGE_CONTEXT_OR_CANONICAL_MISMATCH",expected=forged)

    def test_signed_broker_instance_is_pinned(self):
        forged=replace(self.expected,broker_instance_id="5"*32)
        self.deny("CHALLENGE_CONTEXT_OR_CANONICAL_MISMATCH",expected=forged)

    def test_signed_owner_logon_luid_pinned(self):
        for luid in ((0x12345679, 0x90abcdef), (0x12345678, 0x90abcdee)):
            with self.subTest(luid=luid):
                self.deny("CHALLENGE_CONTEXT_OR_CANONICAL_MISMATCH",
                          expected=replace(self.expected,owner_logon_luid=luid))

    def test_invalid_owner_logon_luid_rejected(self):
        for luid in (None, [], (1,), (1,2,3), (True,2), (-1,2),
                     (1,1<<32), ("1",2)):
            with self.subTest(luid=luid):
                with self.assertRaisesRegex(SignedChallengeDenied,"INVALID_OWNER_LOGON_LUID"):
                    replace(self.expected,owner_logon_luid=luid)

    def test_signed_owner_session_and_sid_pinned(self):
        for changes in ({"owner_session_id":6},
                        {"owner_sid":"S-1-5-21-100-200-301"}):
            with self.subTest(changes=changes):
                self.deny("CHALLENGE_CONTEXT_OR_CANONICAL_MISMATCH",
                          expected=replace(self.expected,**changes))

    def test_signed_service_sid_pinned(self):
        forged=replace(self.expected,service_sid="S-1-5-80-999-200-300-400-500")
        self.deny("CHALLENGE_CONTEXT_OR_CANONICAL_MISMATCH",expected=forged)

    def test_image_release_and_approved_command_pinned(self):
        for name in ("installed_release_sha256","approved_service_image_sha256",
                     "command_snapshot_sha256"):
            with self.subTest(name=name):
                self.deny("CHALLENGE_CONTEXT_OR_CANONICAL_MISMATCH",
                          expected=replace(self.expected,**{name:"8"*64}))

    def test_deadline_cannot_be_extended_by_signed_reuse(self):
        self.deny("CHALLENGE_CONTEXT_OR_CANONICAL_MISMATCH",
                  expected=replace(self.expected,deadline_tick_ms=100001))

    def test_unknown_json_fields_denied(self):
        obj=json.loads(self.payload)
        obj["allow_execute"]=True
        raw=json.dumps(obj,sort_keys=True,separators=(",",":")).encode()
        self.deny("CHALLENGE_CONTEXT_OR_CANONICAL_MISMATCH",payload=raw)

    def test_duplicate_json_field_denied(self):
        raw=self.payload.replace(b'"format":',b'"format":"x","format":',1)
        self.deny("DUPLICATE_CANONICAL_FIELD",payload=raw)

    def test_noncanonical_json_rejected_even_if_semantics_identical(self):
        obj=json.loads(self.payload)
        raw=json.dumps(obj,indent=2,sort_keys=True).encode()
        self.deny("CHALLENGE_CONTEXT_OR_CANONICAL_MISMATCH",payload=raw)

    def test_signing_domain_is_required(self):
        raw=self.key.sign(self.payload,ec.ECDSA(hashes.SHA256()))
        r,s=utils.decode_dss_signature(raw)
        unbound=utils.encode_dss_signature(r,min(s,P256_ORDER-s))
        self.deny("ECDSA_SIGNATURE_INVALID",signature_der=unbound)

    def test_der_signature_truncation(self):
        self.deny("INVALID_DER_SIGNATURE",signature_der=b"\x30\x00")

    def test_wrong_curve_rejected(self):
        other=ec.generate_private_key(ec.SECP384R1())
        public=other.public_key().public_bytes(
            serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
        self.deny("UNACCEPTED_PUBLIC_KEY_ALGORITHM",
                  public_spki_der=public,
                  independently_pinned_spki_sha256=sha256(public).hexdigest())

    def test_ed25519_key_rejected(self):
        other=ed25519.Ed25519PrivateKey.generate()
        public=other.public_key().public_bytes(
            serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
        self.deny("UNACCEPTED_PUBLIC_KEY_ALGORITHM",
                  public_spki_der=public,
                  independently_pinned_spki_sha256=sha256(public).hexdigest())

    def test_high_s_signature_denied_even_if_mathematically_valid(self):
        r,s=utils.decode_dss_signature(self.signature)
        high=utils.encode_dss_signature(r,P256_ORDER-s)
        self.deny("NONCANONICAL_SIGNATURE",signature_der=high)

    def test_public_key_noncanonical_or_invalid_denied(self):
        for bad in (b"",True,None):
            with self.subTest(bad=bad):
                self.deny("INVALID_PUBLIC_KEY_BYTES",public_spki_der=bad)

    def test_malformed_but_matching_pinned_der_denied(self):
        bad=b"x"
        self.deny("INVALID_DER_PUBLIC_KEY",public_spki_der=bad,
                  independently_pinned_spki_sha256=sha256(bad).hexdigest())

    def test_signature_types_bounded(self):
        for bad in (b"",b"x"*129,bytearray(b"x"),None,True):
            with self.subTest(bad=str(bad)[:30]):
                self.deny("INVALID_SIGNATURE_SIZE",signature_der=bad)

    def test_payload_bounds(self):
        for bad in (b"",b"x"*8193,None,True,bytearray(b"x")):
            with self.subTest(type=type(bad)):
                self.deny("INVALID_CHALLENGE_FRAME",payload=bad)

    def test_no_trust_override_fields(self):
        with self.assertRaises(TypeError):
            self.evaluate(service_authenticated=True)

    def test_invalid_nonmatching_or_identical_nonces(self):
        with self.assertRaisesRegex(SignedChallengeDenied,"NONCE_COLLISION"):
            replace(self.expected,service_nonce=self.expected.client_nonce)

    def test_invalid_owner_or_service_sid(self):
        for name,value in (("owner_sid","S-1-5-abc"),
                           ("service_sid","S-1-5-80"),
                           ("owner_sid","S-1-5-80-100-200-300-400-500")):
            with self.subTest(name=name):
                with self.assertRaises(SignedChallengeDenied):
                    replace(self.expected,**{name:value})

    def test_bad_pins_rejected_before_crypto(self):
        for name,value in (("generation","x"*32),("client_nonce","b"*60),
                           ("installed_release_sha256","R"*64),
                           ("connection_epoch",True),("owner_session_id",0),
                           ("deadline_tick_ms",-1)):
            with self.subTest(name=name):
                with self.assertRaises(SignedChallengeDenied):
                    replace(self.expected,**{name:value})

    def test_invalid_pubkey_pin_format(self):
        self.deny("INVALID_PINNED_HASH_OR_NONCE",
                  independently_pinned_spki_sha256="A"*64)

    def test_no_native_keys_or_writer_in_implementation(self):
        code=(Path(__file__).resolve().parents[1]/
              "src/sora_commander_next/offline_b04_signed_challenge_candidate.py").read_text("utf-8")
        for forbidden in ("ec.generate_private_key(", "ctypes.WinDLL(",
                          "OpenSCManager(", "NCryptSignHash(", "sqlite3.connect(",
                          "subprocess.Popen(", "CreateNamedPipe(", "COMMITTED_FRESH"):
            self.assertNotIn(forbidden,code)


if __name__=="__main__":
    unittest.main()
