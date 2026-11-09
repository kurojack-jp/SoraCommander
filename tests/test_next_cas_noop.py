"""Repeated identical edits should never replace a file."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hashlib import sha256
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from unittest import TestCase
from unittest.mock import patch
from sora_commander_next.file_edits import replace_text

class NoopTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
    def test_identical_bytes_never_replace_original(self):
        with inherited_test_temp() as root:
            p=Path(root)/'unchanged.txt'
            data=b'unchanged\r\n'
            p.write_bytes(data)
            digest=sha256(data).hexdigest()
            with patch('sora_commander_next.file_edits.os.replace',side_effect=AssertionError('must not replace')):
                result=replace_text(str(p),(str(Path(root).resolve()),),data.decode(),digest)
            self.assertEqual(result['status'],'UNCHANGED_LOCAL_DEV')
            self.assertEqual(result['sha256'],digest)
            self.assertEqual(p.read_bytes(),data)
            self.assertEqual(list(Path(root).glob('.sora-next-*.tmp')),[])
