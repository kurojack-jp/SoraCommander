"""Trusted Windows Owner-only journal path and deny-by-default checks."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from sora_commander_next.stdio_main import _verify_owner_journal_acls, owner_journal_path
from tests._inherited_test_temp import inherited_test_temp


def _storage_ready():
    """The Owner storage exists only after the first Host or Broker start."""
    local = os.environ.get("LOCALAPPDATA")
    return bool(local) and (Path(local) / "SoraCommanderNext").is_dir()


class JournalPathTests(unittest.TestCase):
    def test_missing_storage_denied(self):
        with inherited_test_temp() as t:
            with self.assertRaisesRegex(RuntimeError, "^Owner journal directory invalid$"):
                owner_journal_path(storage_override=Path(t)/"missing")
    def test_inherited_acl_denied(self):
        with inherited_test_temp() as t:
            with self.assertRaisesRegex(RuntimeError, "^Owner ACL invalid: "):
                owner_journal_path(storage_override=t)
    def test_file_as_directory_denied(self):
        with inherited_test_temp() as t:
            p=Path(t)/"file"
            p.write_text("test")
            with self.assertRaisesRegex(RuntimeError, "^Owner journal directory invalid$"):
                owner_journal_path(storage_override=p)
    @unittest.skipUnless(os.name=="nt" and _storage_ready(),"Windows Owner storage, created on the first start")
    def test_real_protected_owner_storage(self):
        expected=Path(os.environ["LOCALAPPDATA"])/"SoraCommanderNext"/"owner-session-journal.json"
        self.assertEqual(owner_journal_path(),expected)


@unittest.skipUnless(os.name == "nt", "Windows ACL API")
class JournalAclTests(unittest.TestCase):
    def setUp(self):
        import ntsecuritycon
        import win32security as security
        self.security = security
        self.base = Path("synthetic-storage")
        self.paths = [self.base] + [self.base / name for name in (
            "owner-session-journal.json", "owner-output-archive.json",
            "owner-session-journal.json.lock")]
        self.owner = security.ConvertStringSidToSid("S-1-5-21-1-2-3-1001")
        self.system = security.ConvertStringSidToSid("S-1-5-18")
        self.admin = security.ConvertStringSidToSid("S-1-5-32-544")
        self.header = (security.ACCESS_ALLOWED_ACE_TYPE, 0)
        self.full = ntsecuritycon.FILE_ALL_ACCESS
        self.good = [(self.header, self.full, sid)
                     for sid in (self.admin, self.owner, self.system)]
        self.token = Mock()
        self.process = self.enterContext(patch("win32api.GetCurrentProcess"))
        self.open_token = self.enterContext(patch.object(
            security, "OpenProcessToken", return_value=self.token))
        self.token_info = self.enterContext(patch.object(
            security, "GetTokenInformation", return_value=(self.owner, 0)))
        self.read_acl = self.enterContext(patch.object(security, "GetFileSecurity"))
        self.lstat = self.enterContext(patch.object(Path, "lstat", autospec=True))

    def descriptor(self, entries, *, protected=True):
        descriptor = Mock()
        control = self.security.SE_DACL_PROTECTED if protected else 0
        descriptor.GetSecurityDescriptorControl.return_value = (control, 1)
        dacl = None
        if entries is not None:
            dacl = Mock()
            dacl.GetAceCount.return_value = len(entries)
            dacl.GetAce.side_effect = entries.__getitem__
        descriptor.GetSecurityDescriptorDacl.return_value = dacl
        return descriptor

    def test_protected_folder_and_all_present_targets_use_process_token(self):
        import win32con
        # Extra mask bits and propagation flags on explicit grants are permitted.
        entries = [((self.header[0], 3), self.full | 0x01000000, sid)
                   for _, _, sid in self.good]
        self.read_acl.return_value = self.descriptor(entries)
        _verify_owner_journal_acls(self.base)
        self.open_token.assert_called_once_with(self.process.return_value, win32con.TOKEN_QUERY)
        self.token_info.assert_called_once_with(self.token, self.security.TokenUser)
        self.token.Close.assert_called_once_with()
        self.assertEqual(self.read_acl.call_args_list, [
            call(str(path), self.security.DACL_SECURITY_INFORMATION) for path in self.paths])

    def test_only_missing_targets_are_skipped(self):
        self.lstat.side_effect = FileNotFoundError()
        self.read_acl.return_value = self.descriptor(self.good)
        _verify_owner_journal_acls(self.base)
        self.read_acl.assert_called_once_with(str(self.base), self.security.DACL_SECURITY_INFORMATION)
        self.assertEqual(self.lstat.call_args_list, [call(path) for path in self.paths[1:]])

    def test_invalid_acl_at_folder_or_any_present_target(self):
        security = self.security
        rest = self.good[1:]
        wrong = security.ConvertStringSidToSid("S-1-1-0")
        variants = {
            "not protected": self.descriptor(self.good, protected=False),
            "null DACL": self.descriptor(None),
            "two ACEs": self.descriptor(rest),
            "four ACEs": self.descriptor(self.good + self.good[:1]),
            "wrong SID": self.descriptor([(self.header, self.full, wrong)] + rest),
            "duplicate SID": self.descriptor([rest[0]] + rest),
            "inherited": self.descriptor([((self.header[0], security.INHERITED_ACE),
                                            self.full, self.admin)] + rest),
            "deny": self.descriptor([((security.ACCESS_DENIED_ACE_TYPE, 0),
                                      self.full, self.admin)] + rest),
            "object ACE": self.descriptor([((security.ACCESS_ALLOWED_OBJECT_ACE_TYPE, 0),
                                            self.full, None, None, self.admin)] + rest),
            "unknown ACE": self.descriptor([((255, 0),)] + rest),
            "missing right": self.descriptor([(self.header, self.full & ~1, self.admin)] + rest),
        }
        good = self.descriptor(self.good)
        for index, path in enumerate(self.paths):
            for rule, bad in variants.items():
                with self.subTest(path=path, rule=rule):
                    self.read_acl.side_effect = [good] * index + [bad]
                    with self.assertRaisesRegex(RuntimeError, "^Owner ACL invalid: ") as caught:
                        _verify_owner_journal_acls(self.base)
                    message = str(caught.exception)
                    self.assertIn(path.name if index else "the Owner storage folder", message)
                    self.assertNotIn(str(self.base), message)

    def test_acl_api_errors_are_unavailable_on_every_path(self):
        import pywintypes
        error = pywintypes.error(5, "GetFileSecurity", "synthetic error")
        good = self.descriptor(self.good)
        for index, path in enumerate(self.paths):
            with self.subTest(path=path):
                self.read_acl.side_effect = [good] * index + [error]
                with self.assertRaisesRegex(RuntimeError, "^Owner ACL unavailable: "):
                    _verify_owner_journal_acls(self.base)

    def test_presence_query_error_is_unavailable(self):
        self.read_acl.return_value = self.descriptor(self.good)
        self.lstat.side_effect = PermissionError("synthetic metadata error")
        with self.assertRaisesRegex(RuntimeError, "^Owner ACL unavailable: "):
            _verify_owner_journal_acls(self.base)

    def test_unavailable_token_stops_before_reading_acls(self):
        self.token_info.side_effect = OSError("synthetic unavailable")
        with self.assertRaisesRegex(RuntimeError, "^Owner ACL unavailable: "):
            _verify_owner_journal_acls(self.base)
        self.token.Close.assert_called_once_with()
        self.read_acl.assert_not_called()
