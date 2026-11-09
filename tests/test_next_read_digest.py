"""Read-only digest for future compare-and-swap editing."""
from hashlib import sha256
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
import unittest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sora_commander_next.file_reads import read_text_file, ReadBoundaryError


class NextReadDigestTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
    def test_digest_matches_exact_utf8_bytes(self):
        with inherited_test_temp() as root:
            p = Path(root) / 'example.txt'
            raw = 'Next digest check\r\n日本語\n'.encode('utf-8')
            p.write_bytes(raw)
            result = read_text_file(str(p), (str(Path(root).resolve()),))
            self.assertEqual(result['content'], raw.decode('utf-8'))
            self.assertEqual(result['bytes'], len(raw))
            self.assertEqual(result['sha256'], sha256(raw).hexdigest())

    def test_outside_root_still_denied(self):
        with inherited_test_temp() as root, inherited_test_temp() as outside:
            p = Path(outside) / 'outside.txt'
            p.write_text('secret', encoding='utf-8')
            with self.assertRaises(ReadBoundaryError):
                read_text_file(str(p), (str(Path(root).resolve()),))


if __name__ == '__main__':
    unittest.main()
