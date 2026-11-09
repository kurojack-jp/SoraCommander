"""Characterize ACL behavior with disposable files only; no production writes."""
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from hashlib import sha256
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from unittest import TestCase, skipUnless
from sora_commander_next.file_edits import replace_text

@skipUnless(os.name == 'nt','Windows only')
class WindowsACLCharacterization(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
    def test_metadata_and_security_descriptor_on_replacement(self):
        import subprocess
        with inherited_test_temp() as folder:
            root=Path(folder)
            target=root/'acl-test.txt'
            target.write_bytes(b'before')
            def acl():
                cmd=['powershell.exe','-NoProfile','-Command',
                     '$a=Get-Acl -LiteralPath $env:SORA_ACL_TEST_PATH; '
                     '[Console]::WriteLine($a.Sddl)']
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment') as key:
                    module_path, _ = winreg.QueryValueEx(key, 'PSModulePath')
                p=subprocess.run(cmd,capture_output=True,text=True,timeout=15,
                                 env={**os.environ,'SORA_ACL_TEST_PATH':str(target),
                                      'PSModulePath':os.path.expandvars(module_path)})
                if p.returncode != 0:
                    self.skipTest('Get-Acl unavailable')
                return p.stdout.strip()
            before=acl()
            replace_text(str(target),(str(root.resolve()),),'after',sha256(b'before').hexdigest())
            after=acl()
            self.assertTrue(before)
            self.assertTrue(after)
            self.assertEqual(target.read_bytes(),b'after')
            # Difference is recorded as characterization, not yet asserted safe.
            print('WINDOWS_ACL_SDDL_EQUAL='+str(before==after))
