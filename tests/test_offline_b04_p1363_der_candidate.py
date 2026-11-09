import unittest
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from sora_commander_next.offline_b04_p1363_der_candidate import (
    P256_ORDER, SignatureFormatDenied, p1363_to_der, der_to_p1363,
)


class FormatCandidateTests(unittest.TestCase):
    def test_roundtrip(self):
        for r, s in ((1, 1), (P256_ORDER - 1, P256_ORDER // 2), (128, 255)):
            raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
            self.assertEqual(der_to_p1363(p1363_to_der(raw)), raw)

    def test_reject_invalid_raw(self):
        good = (1).to_bytes(32, "big")
        for raw in (b"", b"0" * 63, b"0" * 65, bytearray(b"0" * 64),
                    b"\x00" * 64, (P256_ORDER).to_bytes(32, "big") + good,
                    good + (P256_ORDER // 2 + 1).to_bytes(32, "big")):
            with self.subTest(type=type(raw), length=len(raw)):
                with self.assertRaises(SignatureFormatDenied):
                    p1363_to_der(raw)

    def test_reject_der(self):
        for der in (b"", b"garbage", b"\x30\x00", b"0" * 73,
                    encode_dss_signature(0, 1), encode_dss_signature(1, P256_ORDER // 2 + 1),
                    encode_dss_signature(P256_ORDER, 1)):
            with self.subTest(length=len(der)):
                with self.assertRaises(SignatureFormatDenied):
                    der_to_p1363(der)

    def test_no_authentication_interface(self):
        from sora_commander_next import offline_b04_p1363_der_candidate as m
        self.assertFalse(hasattr(m, "authenticate"))
        self.assertFalse(hasattr(m, "authorize"))
