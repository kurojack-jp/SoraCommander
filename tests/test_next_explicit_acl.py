"""An explicit ACE must stop replacement before staging the synthetic file."""
from hashlib import sha256
import os
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

from sora_commander_next.file_edits import replace_text
from sora_commander_next.file_reads import ReadBoundaryError
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission


@skipUnless(os.name == 'nt', 'Windows only')
class ExplicitACLTests(TestCase):
    def test_explicit_acl_preservation_characterization(self):
        import win32security as sec

        with inherited_test_temp() as folder, canonical_fixture_admission():
            root = Path(folder)
            target = root / 'explicit-acl.txt'
            target.write_bytes(b'old')

            def actual_sddl():
                sections = sec.OWNER_SECURITY_INFORMATION | sec.GROUP_SECURITY_INFORMATION | sec.DACL_SECURITY_INFORMATION
                actual = sec.GetFileSecurity(str(target), sections)
                return sec.ConvertSecurityDescriptorToStringSecurityDescriptor(actual, 1, sections)

            before = actual_sddl()
            # Present an explicit ACE in memory. No filesystem DACL is changed.
            sd = sec.ConvertStringSecurityDescriptorToSecurityDescriptor('D:(A;;FR;;;SY)', 1)
            with patch.object(sec, 'GetNamedSecurityInfo', return_value=sd) as read:
                with self.assertRaisesRegex(ReadBoundaryError, '^unsupported or unverifiable Windows ACL$'):
                    replace_text(str(target), (str(root),), 'new', sha256(b'old').hexdigest())
            read.assert_called_once_with(str(target), sec.SE_FILE_OBJECT, sec.DACL_SECURITY_INFORMATION)
            self.assertEqual(actual_sddl(), before)
            self.assertEqual(target.read_bytes(), b'old')
            self.assertEqual(list(root.iterdir()), [target])
