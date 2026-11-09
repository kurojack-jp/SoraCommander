"""Owner output archive path gate using disposable paths and MOCKED ACL.

No real Windows protected directory, OS security descriptor inspection,
production storage, Owner GUI, Broker, or command launch is performed.
"""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.stdio_main import owner_journal_path


class OwnerArchivePathPreflightTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix="next-output-preflight-")
        self.addCleanup(t.cleanup)
        self.base = Path(t.name).resolve()
        self.acl_result = subprocess.CompletedProcess(
            args=["powershell.exe"], returncode=0,
            stdout="OWNER_ACL_OK\n", stderr="")

    def resolve(self, result=None):
        # Native PowerShell ACL inspection is ONLY MOCKED. Passing this
        # test proves path/response handling, not real Owner ACL quality.
        with patch("subprocess.run",
                   return_value=self.acl_result if result is None else result) as checked:
            resolved = owner_journal_path(storage_override=self.base)
        self.assertEqual(checked.call_count, 1)
        self.assertEqual(checked.call_args.kwargs["env"][
            "SORA_NEXT_VERIFY_STORAGE"], str(self.base))
        return resolved

    def test_existing_regular_archive_and_journal_are_accepted_under_mock_acl(self):
        journal = self.base / "owner-session-journal.json"
        archive = self.base / "owner-output-archive.json"
        journal.write_text('{"version":1,"sessions":[]}', encoding="utf-8")
        archive.write_text('{"version":1,"results":[]}', encoding="utf-8")
        self.assertEqual(self.resolve(), journal)

    def test_preexisting_archive_directory_is_refused(self):
        (self.base / "owner-output-archive.json").mkdir()
        with patch("subprocess.run", return_value=self.acl_result):
            with self.assertRaises(RuntimeError):
                owner_journal_path(storage_override=self.base)

    def test_preexisting_hardlinked_archive_is_refused(self):
        target = self.base / "owner-output-archive.json"
        normal = self.base / "safe-fixture.txt"
        normal.write_text("synthetic only", encoding="utf-8")
        os.link(normal, target)
        with patch("subprocess.run", return_value=self.acl_result):
            with self.assertRaises(RuntimeError):
                owner_journal_path(storage_override=self.base)

    def test_verifier_requires_noninherited_file_acl_in_source_contract(self):
        # Inspect the exact native script passed to the mocked subprocess.
        # This is NOT proof of the real machine's ACL state.
        with patch("subprocess.run", return_value=self.acl_result) as run:
            self.assertEqual(
                owner_journal_path(storage_override=self.base).parent,
                self.base)
        script = run.call_args.args[0][-1]
        self.assertIn("owner-session-journal.json", script)
        self.assertIn("owner-output-archive.json", script)
        self.assertIn("owner-session-journal.json.lock", script)
        self.assertIn("if(-not $fileAcl.AreAccessRulesProtected){exit 9}",
                      script)
        self.assertIn("$r.IsInherited){", script)
        self.assertIn("OWNER_ACL_OK", script)

    def test_preexisting_hardlinked_lock_is_refused(self):
        lock = self.base / "owner-session-journal.json.lock"
        source = self.base / "unrelated-fixture.txt"
        source.write_text("synthetic only", encoding="utf-8")
        os.link(source, lock)
        with patch("subprocess.run", return_value=self.acl_result):
            with self.assertRaises(RuntimeError):
                owner_journal_path(storage_override=self.base)

    def test_incorrect_acl_verification_response_is_fail_closed(self):
        for result in (
            subprocess.CompletedProcess(
                args=[], returncode=7, stdout="", stderr=""),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="NOT_VERIFIED", stderr=""),
        ):
            with self.subTest(code=result.returncode):
                with patch("subprocess.run", return_value=result):
                    with self.assertRaises(RuntimeError):
                        owner_journal_path(storage_override=self.base)


if __name__ == "__main__":
    unittest.main()
