"""DEV_ONLY native BCrypt P-256 -> Python ECDSA verifier cross-check."""
import importlib.util
from pathlib import Path
import struct
import unittest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from sora_commander_next.offline_b04_p1363_der_candidate import P256_ORDER,p1363_to_der,SignatureFormatDenied

fixture=Path(__file__).parent/"fixtures"/"b04_bcrypt_ephemeral.py"
spec=importlib.util.spec_from_file_location("b04_bcrypt_fixture", fixture)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BCryptNativeCrossCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blob,cls.raw,cls.message=module.sample()

    def test_native_public_blob_and_signature(self):
        magic,key_bytes=struct.unpack("<II",self.blob[:8])
        self.assertEqual(magic,0x31534345)  # ECS1 P-256 public blob
        self.assertEqual(key_bytes,32)
        self.assertEqual(len(self.blob),8+2*key_bytes)
        self.assertEqual(len(self.raw),64)
        x=int.from_bytes(self.blob[8:40],"big")
        y=int.from_bytes(self.blob[40:72],"big")
        pub=ec.EllipticCurvePublicNumbers(x,y,ec.SECP256R1()).public_key()
        s=int.from_bytes(self.raw[32:],"big")
        canonical=self.raw[:32]+min(s,P256_ORDER-s).to_bytes(32,"big")
        der=p1363_to_der(canonical)
        pub.verify(der,self.message,ec.ECDSA(hashes.SHA256()))
        with self.assertRaises(InvalidSignature):
            pub.verify(der,self.message+b"!",ec.ECDSA(hashes.SHA256()))
        other=ec.generate_private_key(ec.SECP256R1()).public_key()
        with self.assertRaises(InvalidSignature):
            other.verify(der,self.message,ec.ECDSA(hashes.SHA256()))

    def test_invalid_public_blob_and_raw_signature(self):
        with self.assertRaises((ValueError,AssertionError)):
            malformed=self.blob[:8]+b"\x00"*64
            x=int.from_bytes(malformed[8:40],"big")
            y=int.from_bytes(malformed[40:72],"big")
            ec.EllipticCurvePublicNumbers(x,y,ec.SECP256R1()).public_key()
        with self.assertRaises(SignatureFormatDenied):
            p1363_to_der(self.raw[:63])
