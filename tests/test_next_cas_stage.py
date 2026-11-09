"""Cooperative CAS draft: local-only failure safety, not remote security."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from hashlib import sha256
import unittest
from sora_commander_next.file_edits import replace_text
from sora_commander_next.file_reads import ReadBoundaryError

class LocalCASDraftTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
    def test_replace_with_matching_digest(self):
        with inherited_test_temp() as root:
            p=Path(root)/'file.txt';p.write_bytes(b'before')
            result=replace_text(str(p),(str(Path(root).resolve()),),'after',sha256(b'before').hexdigest())
            self.assertEqual(result['status'],'REPLACED_LOCAL_DEV')
            self.assertEqual(p.read_bytes(),b'after')
            self.assertEqual(result['sha256'],sha256(b'after').hexdigest())
            self.assertEqual(list(Path(root).glob('.sora-next-*.tmp')),[])
    def test_stale_digest_preserves_bytes(self):
        with inherited_test_temp() as root:
            p=Path(root)/'file.txt';p.write_bytes(b'other')
            with self.assertRaises(ReadBoundaryError):
                replace_text(str(p),(str(Path(root).resolve()),),'after',sha256(b'before').hexdigest())
            self.assertEqual(p.read_bytes(),b'other')
    def test_bad_digest_rejected(self):
        with inherited_test_temp() as root:
            p=Path(root)/'file.txt';p.write_bytes(b'old')
            with self.assertRaises(ReadBoundaryError):
                replace_text(str(p),(str(Path(root).resolve()),),'new','Z'*64)
            self.assertEqual(p.read_bytes(),b'old')

if __name__=='__main__': unittest.main()
