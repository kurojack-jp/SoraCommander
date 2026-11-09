"""Pure data-only patch candidate contract; no files touched."""
import unittest
from hashlib import sha256
from sora_commander_next.offline_source_patch_candidate import inspect_patch_bytes

class PurePatchCandidateTests(unittest.TestCase):
    def setUp(self):
        self.before = "x = 1\n".encode()
        self.digest = sha256(self.before).hexdigest()

    def test_digest_and_explicit_no_authority(self):
        result = inspect_patch_bytes(self.before, b"x = 2\n", self.digest)
        self.assertTrue(result.changed)
        self.assertEqual(result.old_sha256, self.digest)
        self.assertEqual(result.authority, "NONE_PREVIEW_ONLY")
    def test_noop(self):
        self.assertFalse(inspect_patch_bytes(self.before, self.before, self.digest).changed)
    def test_stale_fail_closed(self):
        with self.assertRaises(ValueError):
            inspect_patch_bytes(self.before, b"x = 2\n", "0"*64)
    def test_invalid_digest_and_type(self):
        for digest in (None, True, "A"*64, "bad"):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                inspect_patch_bytes(self.before, self.before, digest)
        with self.assertRaises(TypeError):
            inspect_patch_bytes("x=1", b"x=2", self.digest)
    def test_reject_binary_nul_and_overflow(self):
        for before,after in ((self.before,b"\xff"),(self.before,b"\0"),(self.before,b"a"*16385)):
            with self.subTest(after=after[:4]), self.assertRaises((ValueError,UnicodeError)):
                inspect_patch_bytes(before,after,self.digest)
