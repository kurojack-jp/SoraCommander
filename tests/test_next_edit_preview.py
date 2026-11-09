"""Edit-review preview has no side effects and fails on stale input."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from hashlib import sha256
from unittest import TestCase
from sora_commander_next.file_edits import preview_text_edit
from sora_commander_next.file_reads import ReadBoundaryError

class EditPreviewTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
    def test_shows_exact_diff_without_mutation(self):
        with inherited_test_temp() as root:
            p=Path(root)/'demo.txt'; p.write_bytes(b'before\n')
            result=preview_text_edit(str(p),(str(Path(root).resolve()),),'after\n',sha256(b'before\n').hexdigest())
            self.assertTrue(result['changed'])
            self.assertIn('-before', result['diff'])
            self.assertIn('+after', result['diff'])
            self.assertFalse(result['diff_truncated'])
            self.assertEqual(p.read_bytes(),b'before\n')
    def test_stale_input_denied(self):
        with inherited_test_temp() as root:
            p=Path(root)/'demo.txt'; p.write_bytes(b'updated')
            with self.assertRaises(ReadBoundaryError):
                preview_text_edit(str(p),(str(Path(root).resolve()),),'edit',sha256(b'old').hexdigest())
            self.assertEqual(p.read_bytes(),b'updated')
