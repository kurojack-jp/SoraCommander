"""One harmless actual process, Owner DACL protected temp storage, cold reload.

Not production Owner path, not a service/tunnel, no Windows reboot, no
Owner GUI authentication. Does not exercise blocked journal concurrency test.
"""
import base64
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.commands import Command
from sora_commander_next.jobs import JobManager
from sora_commander_next.owner_stage_acl import (
    verify_owner_stage_after_replace, _sids,
)


@unittest.skipUnless(os.name == "nt", "Windows-only disposable ACL integration")
class ProtectedJobRecoveryWindowsTests(unittest.TestCase):
    def test_real_job_three_protected_files_then_cold_read_without_replay(self):
        import ntsecuritycon
        import win32security
        with tempfile.TemporaryDirectory(prefix="next-owner-recovery-") as d:
            root = Path(d).resolve()
            acl = win32security.ACL()
            for sid in _sids():
                acl.AddAccessAllowedAceEx(
                    win32security.ACL_REVISION_DS,
                    win32security.OBJECT_INHERIT_ACE
                    | win32security.CONTAINER_INHERIT_ACE,
                    ntsecuritycon.FILE_ALL_ACCESS, sid)
            descriptor = win32security.SECURITY_DESCRIPTOR()
            descriptor.SetSecurityDescriptorDacl(1, acl, 0)
            win32security.SetFileSecurity(
                str(root),
                win32security.DACL_SECURITY_INFORMATION
                | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                descriptor)
            journal = root / "owner-session-journal.json"
            archive = root / "owner-output-archive.json"
            lock = root / "owner-session-journal.json.lock"
            run = JobManager(
                journal_path=journal, output_archive_path=archive,
                protected_stage=True)
            request = Command(
                sys.executable,
                ("-I", "-S", "-c",
                 "import sys;"
                 "sys.stdout.write('OWNER-PROTECTED-OK\\n');sys.stdout.flush();"
                 "sys.stderr.write('OWNER-PROTECTED-ERR\\n');sys.stderr.flush()"),
                str(root), timeout=5, request_id="synthetic-once-only")
            try:
                started = run.start(request)
                self.assertEqual(started["status"], "RUNNING")
                observed = run.wait(started["session_id"])
                self.assertEqual(observed["status"], "COMPLETED")
                self.assertEqual(observed["exit_code"], 0)
                with run._lock:
                    supervisor = run._jobs[started["session_id"]].thread
                supervisor.join(timeout=3)
                self.assertFalse(supervisor.is_alive())
            finally:
                run.close()
            for file in (journal, archive, lock):
                self.assertTrue(file.exists(), str(file))
                self.assertTrue(verify_owner_stage_after_replace(file))
            cold = JobManager(
                journal_path=journal, output_archive_path=archive,
                protected_stage=True)
            try:
                self.assertEqual(cold.list_sessions()["count"], 0)
                self.assertEqual(cold.previous_sessions()["count"], 1)
                self.assertEqual(cold.previous_sessions()["sessions"][0]["status"],
                                 "COMPLETED")
                saved = cold.previous_output(started["session_id"])
                self.assertEqual(saved["status"], "COMPLETED")
                self.assertEqual(saved["exit_code"], 0)
                self.assertEqual(saved["request_id"], "synthetic-once-only")
                self.assertEqual(
                    b"".join(base64.b64decode(x["data_base64"])
                             for x in saved["chunks"] if x["stream"] == "stdout"),
                    b"OWNER-PROTECTED-OK\r\n")
                self.assertEqual(
                    b"".join(base64.b64decode(x["data_base64"])
                             for x in saved["chunks"] if x["stream"] == "stderr"),
                    b"OWNER-PROTECTED-ERR\r\n")
                self.assertEqual(cold.list_sessions()["count"], 0)
            finally:
                cold.close()


if __name__ == "__main__":
    unittest.main()
