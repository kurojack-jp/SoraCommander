"""Read-only ACL verification and replacement characterization on disposable files."""
import os
from hashlib import sha256
from pathlib import Path
from unittest import TestCase, skipUnless

from sora_commander_next import file_edits, scoped_dev_edits
from sora_commander_next.file_reads import ReadBoundaryError
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission


@skipUnless(os.name == 'nt', 'Windows only')
class WindowsACLCharacterization(TestCase):
    def descriptor(self, target, *, include_group=False):
        import win32security as sec

        sections = sec.OWNER_SECURITY_INFORMATION | sec.DACL_SECURITY_INFORMATION
        if include_group:
            sections |= sec.GROUP_SECURITY_INFORMATION
        sd = sec.GetNamedSecurityInfo(str(target), sec.SE_FILE_OBJECT, sections)
        return sd, sec.ConvertSecurityDescriptorToStringSecurityDescriptor(sd, 1, sections)

    def test_read_only_acl_gates_on_inherited_file(self):
        import win32api
        import win32con
        import win32security as sec

        token = sec.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid = sec.ConvertSidToStringSid(sec.GetTokenInformation(token, sec.TokenUser)[0])
        finally:
            token.Close()
        trusted = {sid, 'S-1-5-18', 'S-1-5-32-544'}
        with inherited_test_temp() as folder:
            target = Path(folder) / 'read-only-acl.txt'
            target.write_bytes(b'fixture')
            file_edits._require_inherited_windows_acl(target)
            # The real ancestors may legitimately fail the scoped write policy.
            # Independently read their named descriptors and predict that decision.
            records = []
            admissible = True
            for path in (target, *target.parents):
                sd, record = self.descriptor(path)
                records.append(record)
                owner = sec.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner())
                acl = sd.GetSecurityDescriptorDacl()
                if owner not in trusted or acl is None or not acl.GetAceCount():
                    admissible = False
                    break
                inherited_flags = []
                for i in range(acl.GetAceCount()):
                    ace = acl.GetAce(i)
                    kind, flags = ace[0]
                    inherited = bool(flags & sec.INHERITED_ACE)
                    inherited_flags.append(inherited)
                    if (kind != sec.ACCESS_ALLOWED_ACE_TYPE or len(ace) != 3 or flags & ~0x1f
                            or (path == target and not inherited) or ace[1] & ~0x1F01FF
                            or (ace[1] & 0xD0156 and sec.ConvertSidToStringSid(ace[2]) not in trusted)):
                        admissible = False
                        break
                if not admissible or inherited_flags != sorted(inherited_flags):
                    admissible = False
                    break
            if admissible:
                self.assertEqual(scoped_dev_edits._acl_snapshot(target, sid), tuple(records))
            else:
                with self.assertRaisesRegex(ReadBoundaryError, '^abnormal or unverifiable ACL$') as denied:
                    scoped_dev_edits._acl_snapshot(target, sid)
                # A policy refusal must not hide a Win32 access or decoding error.
                self.assertIsInstance(denied.exception.__cause__, ReadBoundaryError)
                self.assertIsNone(denied.exception.__cause__.__cause__)
            self.assertEqual(target.read_bytes(), b'fixture')

    def test_metadata_and_security_descriptor_on_replacement(self):
        with inherited_test_temp() as folder, canonical_fixture_admission():
            root = Path(folder)
            target = root / 'acl-test.txt'
            target.write_bytes(b'before')
            _, before = self.descriptor(target, include_group=True)
            file_edits.replace_text(str(target), (str(root),), 'after', sha256(b'before').hexdigest())
            _, after = self.descriptor(target, include_group=True)
            self.assertTrue(before)
            self.assertTrue(after)
            self.assertEqual(target.read_bytes(), b'after')
            print('WINDOWS_ACL_SDDL_EQUAL=' + str(before == after))
