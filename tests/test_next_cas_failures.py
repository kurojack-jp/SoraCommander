"""Failure-injection tests for development-only CAS; no MCP write exposure."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hashlib import sha256
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from unittest import TestCase
from unittest.mock import patch
from sora_commander_next.file_edits import replace_text
from sora_commander_next.file_reads import ReadBoundaryError

class CASFailureTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
    def test_atomic_replace_failure_keeps_original_and_removes_staging(self):
        with inherited_test_temp() as root:
            p=Path(root)/'file.txt';p.write_bytes(b'original')
            with patch('sora_commander_next.file_edits.os.replace', side_effect=OSError('simulated')):
                with self.assertRaises(OSError):
                    replace_text(str(p),(str(Path(root).resolve()),),'replacement',sha256(b'original').hexdigest())
            self.assertEqual(p.read_bytes(),b'original')
            self.assertEqual(list(Path(root).glob('.sora-next-*.tmp')),[])

    def test_change_before_final_check_prevents_overwrite(self):
        with inherited_test_temp() as root:
            p=Path(root)/'file.txt';p.write_bytes(b'original')
            original_check=__import__('sora_commander_next.file_edits',fromlist=['_check'])._check
            count=[0]
            def check_and_change(path,roots):
                count[0]+=1
                if count[0]==2:
                    p.write_bytes(b'changed externally')
                return original_check(path,roots)
            with patch('sora_commander_next.file_edits._check',side_effect=check_and_change):
                with self.assertRaises(ReadBoundaryError):
                    replace_text(str(p),(str(Path(root).resolve()),),'replacement',sha256(b'original').hexdigest())
            self.assertEqual(p.read_bytes(),b'changed externally')
            self.assertEqual(list(Path(root).glob('.sora-next-*.tmp')),[])
