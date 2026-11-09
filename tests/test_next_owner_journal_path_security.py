"""Trusted Windows Owner-only journal path and deny-by-default checks."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from sora_commander_next.stdio_main import owner_journal_path


def _storage_ready():
    """The Owner storage exists only after the first Host or Broker start."""
    local = os.environ.get("LOCALAPPDATA")
    return bool(local) and (Path(local) / "SoraCommanderNext").is_dir()


class JournalPathTests(unittest.TestCase):
    def test_missing_storage_denied(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(RuntimeError):
                owner_journal_path(storage_override=Path(t)/"missing")
    def test_inherited_acl_denied(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(RuntimeError):
                owner_journal_path(storage_override=t)
    def test_file_as_directory_denied(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"file"
            p.write_text("test")
            with self.assertRaises(RuntimeError):
                owner_journal_path(storage_override=p)
    @unittest.skipUnless(os.name=="nt" and _storage_ready(),"Windows Owner storage, created on the first start")
    def test_real_protected_owner_storage(self):
        expected=Path(os.environ["LOCALAPPDATA"])/"SoraCommanderNext"/"owner-session-journal.json"
        self.assertEqual(owner_journal_path(),expected)

    @unittest.skipUnless(os.name=="nt" and _storage_ready(),"Windows Owner storage, created on the first start")
    def test_untrusted_inherited_module_path_does_not_bypass_acl_check(self):
        from unittest.mock import patch
        expected=Path(os.environ["LOCALAPPDATA"])/"SoraCommanderNext"/"owner-session-journal.json"
        with patch.dict(os.environ, {"PSModulePath":"SYNTHETIC_INVALID_MODULE_PATH"}):
            self.assertEqual(owner_journal_path(),expected)

    @unittest.skipUnless(os.name=="nt" and _storage_ready(),"Windows Owner storage, created on the first start")
    def test_missing_machine_module_path_fails_closed_without_acl_runner(self):
        from unittest.mock import patch
        with patch("winreg.QueryValueEx",side_effect=OSError("synthetic unavailable")), \
             patch("subprocess.run") as runner:
            with self.assertRaisesRegex(RuntimeError,"Owner ACL unavailable"):
                owner_journal_path()
            runner.assert_not_called()
