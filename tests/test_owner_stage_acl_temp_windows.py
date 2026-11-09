"""Disposable Windows staged-file ACL and atomic archive/journal acceptance.

NOT production Owner storage, not Owner identity/presence attestation. Windows
ACLs are only changed on a freshly created test fixture in TemporaryDirectory.
No privileged install, daemon, tunnel or job execution.
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.offline_result_archive import (
    write_result_checkpoint, read_previous_results)
from sora_commander_next.session_journal import (
    write_snapshot, read_previous_snapshot)
from sora_commander_next.owner_stage_acl import (
    protect_owner_stage_local, verify_owner_stage_after_replace,
    OwnerStageAclError)


@unittest.skipUnless(os.name == "nt", "Windows Owner ACL tests only")
class OwnerStageWindowsTempTests(unittest.TestCase):
    def setUp(self):
        import win32security
        import ntsecuritycon
        folder = tempfile.TemporaryDirectory(prefix="next-owner-acl-temp-")
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name).resolve()
        from sora_commander_next.owner_stage_acl import _sids
        acl = win32security.ACL()
        for sid in _sids():
            acl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION_DS,
                win32security.OBJECT_INHERIT_ACE |
                win32security.CONTAINER_INHERIT_ACE,
                ntsecuritycon.FILE_ALL_ACCESS, sid)
        sd = win32security.SECURITY_DESCRIPTOR()
        sd.SetSecurityDescriptorDacl(1, acl, 0)
        win32security.SetFileSecurity(
            str(self.home),
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            sd)

    def output_record(self, version=0):
        return {
            "request_id": "synthetic-owner-test",
            "session_id": "b" * 48,
            "status": "RUNNING" if version == 0 else "COMPLETED",
            "exit_code": None if version == 0 else 0,
            "captured_bytes": 0, "output_incomplete": False,
            "saved_at": 1000000 + version,
            "archived_truncated": False, "chunks": [],
        }

    def journal_record(self, version=0):
        return [{
            "session_id": "c" * 48,
            "status": "RUNNING" if version == 0 else "COMPLETED",
            "pid": 123, "exit_code": None if version == 0 else 0,
            "captured_bytes": 0, "output_incomplete": False,
        }]

    def test_journal_lock_file_protected_and_reused(self):
        journal = self.home / "owner-session-journal.json"
        lock = self.home / "owner-session-journal.json.lock"
        write_snapshot(journal, self.journal_record(0), protected_stage=True)
        self.assertTrue(verify_owner_stage_after_replace(lock))
        before = lock.stat()
        write_snapshot(journal, self.journal_record(1), protected_stage=True)
        self.assertTrue(verify_owner_stage_after_replace(lock))
        after = lock.stat()
        self.assertEqual((before.st_dev, before.st_ino),
                         (after.st_dev, after.st_ino))

    def test_unprotected_existing_lock_is_denied_without_repair(self):
        journal = self.home / "owner-session-journal.json"
        lock = self.home / "owner-session-journal.json.lock"
        lock.write_bytes(b"synthetic")
        original = lock.read_bytes()
        with self.assertRaises(OwnerStageAclError):
            write_snapshot(journal, self.journal_record(0), protected_stage=True)
        self.assertEqual(lock.read_bytes(), original)
        self.assertFalse(journal.exists())

    def test_archive_protected_across_two_real_python_replacements(self):
        path = self.home / "owner-output-archive.json"
        for n in range(2):
            write_result_checkpoint(path, self.output_record(n),
                                    now=1000000+n, protected_stage=True)
            self.assertTrue(verify_owner_stage_after_replace(path))
        self.assertEqual(
            read_previous_results(path, now=1000002)["results"][0]["status"],
            "COMPLETED")

    def test_metadata_journal_protected_across_two_replacements(self):
        path = self.home / "owner-session-journal.json"
        for n in range(2):
            write_snapshot(path, self.journal_record(n), protected_stage=True)
            self.assertTrue(verify_owner_stage_after_replace(path))
        self.assertEqual(
            read_previous_snapshot(path)["sessions"][0]["status"],
            "COMPLETED")

    def test_rejected_acl_preparation_preserves_prior_archive_bytes(self):
        path = self.home / "owner-output-archive.json"
        write_result_checkpoint(path, self.output_record(0),
                                now=1000000, protected_stage=True)
        previous = path.read_bytes()
        with patch("sora_commander_next.owner_stage_acl.protect_owner_stage_local",
                   side_effect=OwnerStageAclError("simulated stage denial")):
            with self.assertRaises(OwnerStageAclError):
                write_result_checkpoint(path, self.output_record(1),
                                        now=1000001, protected_stage=True)
        self.assertEqual(path.read_bytes(), previous)
        self.assertTrue(verify_owner_stage_after_replace(path))

    def test_archive_post_replace_verification_failure_is_reported(self):
        # A post-replace error occurs AFTER the atomic swap. Report error,
        # never claim that the previous bytes survived.
        path = self.home / "owner-output-archive.json"
        write_result_checkpoint(path, self.output_record(0),
                                now=1000000, protected_stage=True)
        previous = path.read_bytes()
        with patch("sora_commander_next.owner_stage_acl.verify_owner_stage_after_replace",
                   side_effect=OwnerStageAclError("verification failure")):
            with self.assertRaises(OwnerStageAclError):
                write_result_checkpoint(path, self.output_record(1),
                                        now=1000001, protected_stage=True)
        self.assertNotEqual(path.read_bytes(), previous)
        self.assertTrue(verify_owner_stage_after_replace(path))
        self.assertEqual(
            read_previous_results(path, now=1000002)["results"][0]["status"],
            "COMPLETED")

    def test_journal_post_replace_verification_failure_is_reported(self):
        path = self.home / "owner-session-journal.json"
        write_snapshot(path, self.journal_record(0), protected_stage=True)
        previous = path.read_bytes()
        # Only fail the final journal verification. The lock-file preflight
        # must continue to use the real ACL verifier.
        import sora_commander_next.owner_stage_acl as owner_acl
        original_verify = owner_acl.verify_owner_stage_after_replace
        def verify_journal_only(target):
            if Path(target) == path:
                raise OwnerStageAclError("verification failure")
            return original_verify(target)
        with patch("sora_commander_next.owner_stage_acl.verify_owner_stage_after_replace",
                   side_effect=verify_journal_only):
            with self.assertRaises(OwnerStageAclError):
                write_snapshot(path, self.journal_record(1),
                               protected_stage=True)
        self.assertNotEqual(path.read_bytes(), previous)
        self.assertTrue(verify_owner_stage_after_replace(path))
        self.assertEqual(
            read_previous_snapshot(path)["sessions"][0]["status"],
            "COMPLETED")

    def test_stage_validation_refuses_wrong_filename_and_directory(self):
        stage = self.home / "unauthorized.tmp"
        stage.write_text("synthetic", encoding="utf-8")
        with self.assertRaises(OwnerStageAclError):
            protect_owner_stage_local(stage)
        self.assertEqual(stage.read_text(encoding="utf-8"), "synthetic")
        with self.assertRaises(OwnerStageAclError):
            protect_owner_stage_local(self.home)

    def test_rejected_acl_preparation_preserves_prior_journal_bytes(self):
        path = self.home / "owner-session-journal.json"
        write_snapshot(path, self.journal_record(0), protected_stage=True)
        previous = path.read_bytes()
        with patch("sora_commander_next.owner_stage_acl.protect_owner_stage_local",
                   side_effect=OwnerStageAclError("simulated stage denial")):
            with self.assertRaises(OwnerStageAclError):
                write_snapshot(path, self.journal_record(1),
                               protected_stage=True)
        self.assertEqual(path.read_bytes(), previous)
        self.assertTrue(verify_owner_stage_after_replace(path))


if __name__ == "__main__":
    unittest.main()
