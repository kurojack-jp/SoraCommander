"""Windows-only individual ACL preservation characterization on disposable files."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hashlib import sha256
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
import subprocess
from sora_commander_next.file_edits import replace_text
from sora_commander_next.file_reads import ReadBoundaryError

@skipUnless(os.name == 'nt', 'Windows only')
class ExplicitACLTests(TestCase):
    def test_explicit_acl_preservation_characterization(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'explicit-acl.txt'
            target.write_bytes(b'old')
            script = r"""
$ErrorActionPreference='Stop'
$p=$env:SORA_ACL_TEST_PATH
$a=Get-Acl -LiteralPath $p
$rule=New-Object System.Security.AccessControl.FileSystemAccessRule(
  [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,
  'ReadData','Allow')
$a.AddAccessRule($rule)
Set-Acl -LiteralPath $p -AclObject $a
"""
            env={**os.environ,'SORA_ACL_TEST_PATH':str(target)}
            applied=subprocess.run(['powershell.exe','-NoProfile','-Command',script],
                                   capture_output=True,text=True,timeout=15,env=env)
            if applied.returncode:
                self.skipTest('Cannot assign individual ACL on test file')
            def sddl():
                p=subprocess.run(['powershell.exe','-NoProfile','-Command',
                    '(Get-Acl -LiteralPath $env:SORA_ACL_TEST_PATH).Sddl'],
                    capture_output=True,text=True,timeout=15,env=env)
                self.assertEqual(p.returncode,0,p.stderr)
                return p.stdout.strip()
            before=sddl()
            with self.assertRaises(ReadBoundaryError):
                replace_text(str(target),(str(root.resolve()),),'new',sha256(b'old').hexdigest())
            after=sddl()
            self.assertEqual(before,after)
            self.assertEqual(target.read_bytes(),b'old')
