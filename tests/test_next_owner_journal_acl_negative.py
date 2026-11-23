"""Existing-file ACL must fail closed on a disposable Owner-only folder."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from sora_commander_next.stdio_main import owner_journal_path
from sora_commander_next.session_journal import write_snapshot


def _trustee_category(trustee, current_user):
    import win32security

    sid = win32security.ConvertSidToStringSid(trustee)
    if sid.startswith("S-1-5-21-") and sid.endswith("-500"):
        return "LA (RID 500)"
    if sid == current_user:
        return "current user"
    if sid.startswith("S-1-5-21-"):
        return "other domain SID"

    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(trustee, False)
    owner_sddl = win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
        descriptor, win32security.SDDL_REVISION_1, win32security.OWNER_SECURITY_INFORMATION)
    alias = owner_sddl.removeprefix("O:")
    if alias in ("SY", "BA"):
        return alias
    if len(alias) == 2 and alias.isascii() and alias.isalpha() and alias.isupper():
        return f"well-known {alias}"
    return "well-known <unmapped>"


def _describe_dacl(path, current_user):
    import win32security

    try:
        descriptor = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION)
        control, _ = descriptor.GetSecurityDescriptorControl()
        protected = bool(control & win32security.SE_DACL_PROTECTED)
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:
            return f"protected={protected}, ace_count=0 (null DACL)"
        count = dacl.GetAceCount()
        lines = [f"protected={protected}, ace_count={count}"]
        for index in range(count):
            ace = dacl.GetAce(index)
            kind, flags = ace[0]
            mask = ace[1]
            category = _trustee_category(ace[-1], current_user)
            lines.append(
                f"  ACE[{index}] type={kind} flags=0x{flags:02X} "
                f"mask=0x{mask & 0xFFFFFFFF:08X} trustee={category}")
        return "\n".join(lines)
    except win32security.error as exc:
        return f"DACL diagnostics unavailable (Win32 error {exc.winerror})"


@unittest.skipUnless(os.name=="nt","Windows ACL tests")
class FileAclNegativeTests(unittest.TestCase):
    def test_dacl_diagnostic_uses_trustee_categories(self):
        import win32security

        current_user = "S-1-5-21-1-2-3-1001"
        descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            "D:P(A;OICI;FA;;;SY)(A;;FA;;;BA)"
            "(A;;FR;;;S-1-5-21-1-2-3-500)"
            f"(A;;FR;;;{current_user})(A;;FR;;;S-1-5-21-1-2-3-1002)"
            "(D;;FR;;;AU)",
            win32security.SDDL_REVISION_1,
        )
        with patch.object(win32security, "GetFileSecurity", return_value=descriptor):
            self.assertEqual(_describe_dacl(Path("synthetic"), current_user), "\n".join((
                "protected=True, ace_count=6",
                "  ACE[0] type=0 flags=0x03 mask=0x001F01FF trustee=SY",
                "  ACE[1] type=0 flags=0x00 mask=0x001F01FF trustee=BA",
                "  ACE[2] type=0 flags=0x00 mask=0x00120089 trustee=LA (RID 500)",
                "  ACE[3] type=0 flags=0x00 mask=0x00120089 trustee=current user",
                "  ACE[4] type=0 flags=0x00 mask=0x00120089 trustee=other domain SID",
                "  ACE[5] type=1 flags=0x00 mask=0x00120089 trustee=well-known AU",
            )))

    def test_extra_file_ace_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/"protected"
            directory.mkdir()
            owner=subprocess.run(["whoami","/user"],capture_output=True,text=True,check=True).stdout
            import ctypes
            # Use icacls only on the new disposable directory, never on real Owner storage.
            sid=owner.split("S-1-5-21-")[1].split()[0]
            sid="S-1-5-21-"+sid
            def acl(*args):
                return subprocess.run(["icacls",str(directory),*args],capture_output=True,text=True)
            self.assertEqual(acl("/inheritance:r").returncode,0)
            for principal in ("*"+sid,"*S-1-5-18","*S-1-5-32-544"):
                self.assertEqual(acl("/grant:r",principal+":(OI)(CI)F").returncode,0)
            journal=directory/"owner-session-journal.json"
            # Owner storage requires explicit protected stage/lock ACLs.
            # Plain write_snapshot (unprotected DEV mode) must not pass this check.
            write_snapshot(journal, [], protected_stage=True)
            try:
                self.assertEqual(owner_journal_path(storage_override=directory),journal)
            except (AssertionError, RuntimeError) as exc:
                exc.add_note("folder DACL: " + _describe_dacl(directory, sid))
                exc.add_note("journal file DACL: " + _describe_dacl(journal, sid))
                raise
            bad=subprocess.run(["icacls",str(journal),"/grant","*S-1-5-11:(R)"],capture_output=True,text=True)
            self.assertEqual(bad.returncode,0,bad.stderr)
            with self.assertRaises(RuntimeError):
                owner_journal_path(storage_override=directory)
