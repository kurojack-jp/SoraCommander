"""Characterize Windows file metadata under local-only staged replacement."""
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from hashlib import sha256
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from unittest import TestCase, skipUnless
from sora_commander_next.file_edits import replace_text

@skipUnless(os.name == 'nt', 'Windows only')
class WindowsMetadataTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
    def test_replacement_keeps_target_contents_and_sibling(self):
        with inherited_test_temp() as root:
            folder=Path(root)
            target=folder/'target.txt'; sibling=folder/'sibling.txt'
            target.write_bytes(b'old'); sibling.write_bytes(b'keep')
            result=replace_text(str(target),(str(folder.resolve()),),'new',sha256(b'old').hexdigest())
            self.assertEqual(result['status'],'REPLACED_LOCAL_DEV')
            self.assertEqual(target.read_bytes(),b'new')
            self.assertEqual(sibling.read_bytes(),b'keep')
    def test_staging_changes_identity_not_inplace(self):
        with inherited_test_temp() as root:
            folder=Path(root); target=folder/'target.txt';target.write_bytes(b'old')
            old_ino=target.stat().st_ino
            replace_text(str(target),(str(folder.resolve()),),'new',sha256(b'old').hexdigest())
            self.assertNotEqual(target.stat().st_ino,old_ino)
