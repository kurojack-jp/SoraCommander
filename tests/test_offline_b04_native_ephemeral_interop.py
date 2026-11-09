"""DEV_ONLY isolated native Windows .NET ECDSA -> Python cryptography interoperability.
No persistent key, service operation, publisher trust or authentication admitted.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import unittest

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from sora_commander_next.offline_b04_p1363_der_candidate import (
    P256_ORDER, SignatureFormatDenied, der_to_p1363, p1363_to_der,
)

SCRIPT = Path(__file__).with_name("fixtures") / "b04_ephemeral_ecdsa.ps1"


@unittest.skipUnless(os.name == "nt", "Windows only")
class NativeEphemeralInterop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            completed = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(SCRIPT)],
                capture_output=True, text=True, timeout=25, check=True,
            )
        except (FileNotFoundError, subprocess.SubprocessError) as e:
            raise AssertionError("Explicit PowerShell 7 interoperability prerequisite failed") from e
        cls.sample = json.loads(completed.stdout.strip())

    def test_native_p1363_signature_can_verify_as_der(self):
        sample = self.sample
        data = base64.b64decode(sample["data"], validate=True)
        spki = base64.b64decode(sample["spki"], validate=True)
        raw = base64.b64decode(sample["signature"], validate=True)
        self.assertEqual(len(raw), 64)
        self.assertEqual(sample["length"], 64)
        pub = serialization.load_der_public_key(spki)
        self.assertIsInstance(pub, ec.EllipticCurvePublicKey)
        self.assertIsInstance(pub.curve, ec.SECP256R1)
        r = int.from_bytes(raw[:32], "big")
        s = int.from_bytes(raw[32:], "big")
        self.assertTrue(1 <= r < P256_ORDER and 1 <= s < P256_ORDER)
        # Windows may return high-S: low-S normalization preserves ECDSA mathematical validity.
        canonical = raw[:32] + min(s, P256_ORDER - s).to_bytes(32, "big")
        der = p1363_to_der(canonical)
        self.assertEqual(der_to_p1363(der), canonical)
        pub.verify(der, data, ec.ECDSA(hashes.SHA256()))
        with self.assertRaises(Exception):
            pub.verify(der, data + b"tampered", ec.ECDSA(hashes.SHA256()))
        # Unsupported high-S remains denied by production-facing strict converter.
        if s > P256_ORDER // 2:
            with self.assertRaises(SignatureFormatDenied):
                p1363_to_der(raw)

    def test_no_private_key_export(self):
        self.assertEqual(set(self.sample), {"data","spki","signature","length","provider_type"})
        self.assertNotIn("PRIVATE KEY", json.dumps(self.sample))

    def test_wrong_public_key_and_curve_rejected(self):
        sample = self.sample
        data = base64.b64decode(sample["data"], validate=True)
        raw = base64.b64decode(sample["signature"], validate=True)
        s = int.from_bytes(raw[32:], "big")
        der = p1363_to_der(raw[:32] + min(s, P256_ORDER - s).to_bytes(32, "big"))
        other = ec.generate_private_key(ec.SECP256R1()).public_key()
        with self.assertRaises(InvalidSignature):
            other.verify(der, data, ec.ECDSA(hashes.SHA256()))
        wrong_curve = ec.generate_private_key(ec.SECP384R1()).public_key()
        self.assertNotIsInstance(wrong_curve.curve, ec.SECP256R1)
        with self.assertRaises(InvalidSignature):
            wrong_curve.verify(der, data, ec.ECDSA(hashes.SHA256()))

    def test_corrupted_signature_rejected(self):
        sample = self.sample
        data = base64.b64decode(sample["data"], validate=True)
        spki = base64.b64decode(sample["spki"], validate=True)
        raw = base64.b64decode(sample["signature"], validate=True)
        s = int.from_bytes(raw[32:], "big")
        der = p1363_to_der(raw[:32] + min(s, P256_ORDER - s).to_bytes(32, "big"))
        pub = serialization.load_der_public_key(spki)
        damaged = bytearray(der)
        damaged[-1] ^= 1
        with self.assertRaises((InvalidSignature, ValueError)):
            pub.verify(bytes(damaged), data, ec.ECDSA(hashes.SHA256()))

    def test_provider_type_observation_is_not_trust(self):
        provider = self.sample["provider_type"]
        self.assertIsInstance(provider, str)
        self.assertTrue(provider and len(provider) < 128)
        self.assertNotIn("authentication", self.sample)
        self.assertNotIn("trusted", self.sample)
