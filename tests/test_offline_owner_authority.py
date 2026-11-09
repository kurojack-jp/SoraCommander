"""DEV-only local Owner authority acceptance with SYNTHETIC UI child fixtures.

The approve/deny fixtures are not human Owner clicks. Actual Windows token,
WTS guard, OS child UI process and real Windows JobManager are exercised.
Never start the Tk GUI automatically in tests or promote this to live IPC.
"""
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sora_commander_next.commands import Command
from sora_commander_next.offline_owner_authority import DevOwnerAuthority

ROOT = Path(__file__).resolve().parents[1]
APPROVE = ROOT / "tests" / "fixtures" / "phase9_dialog_approve.py"
DENY = ROOT / "tests" / "fixtures" / "phase9_dialog_deny.py"
BAD = ROOT / "tests" / "fixtures" / "phase9_dialog_bad.py"


@unittest.skipUnless(os.name == "nt", "Windows Owner-local GUI acceptance")
class OwnerAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sora-owner-authority-")
        self.addCleanup(self.temp.cleanup)
        self.code = "print('owner-authority-one-job')"
        self.command = Command(
            sys.executable, ("-I", "-S", "-c", self.code),
            self.temp.name, timeout=4.0, output_limit=2048,
            request_id=secrets.token_hex(16))
        self.authority = DevOwnerAuthority(fixed_command=self.command)
        self.addCleanup(self.authority.close)

    def test_synthetic_ui_approve_starts_one_job_only(self):
        pending = self.authority.stage_local()
        self.assertEqual(len(pending), 32)
        self.assertEqual(self.authority._jobs.list_sessions()["count"], 0)
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT", APPROVE):
            self.assertEqual(self.authority.review_once_local(), "RUNNING")
        result = self.authority.wait_local(seconds=5.0)
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["request_id"], self.command.request_id)
        self.assertEqual(result["stdout"].strip(), "owner-authority-one-job")
        self.assertEqual(self.authority._jobs.list_sessions()["count"], 1)
        with self.assertRaises(PermissionError):
            self.authority.review_once_local()
        with self.assertRaises(PermissionError):
            self.authority.stage_local()
        self.assertEqual(self.authority._jobs.list_sessions()["count"], 1)

    def test_synthetic_ui_deny_never_launches(self):
        self.authority.stage_local()
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT", DENY):
            self.assertEqual(self.authority.review_once_local(), "DENIED")
        self.assertEqual(self.authority._jobs.list_sessions()["count"], 0)
        with self.assertRaises(PermissionError):
            self.authority.wait_local()
        with self.assertRaises(PermissionError):
            self.authority.review_once_local()

    def test_corrupt_ui_reply_does_not_start_and_cannot_retry(self):
        self.authority.stage_local()
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT", BAD):
            self.assertEqual(self.authority.review_once_local(), "OUTCOME_UNKNOWN")
        self.assertEqual(self.authority._jobs.list_sessions()["count"], 0)
        with self.assertRaises(PermissionError):
            self.authority.review_once_local()

    def test_missing_owner_identity_denied_before_launch(self):
        with patch.object(self.authority._guard, "verify",
                          side_effect=PermissionError("simulated lock")):
            with self.assertRaises(PermissionError):
                self.authority.stage_local()
        self.assertEqual(self.authority._jobs.list_sessions()["count"], 0)

    def test_owned_session_requires_verified_owner_for_result(self):
        self.authority.stage_local()
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT", APPROVE):
            self.assertEqual(self.authority.review_once_local(), "RUNNING")
        with patch.object(self.authority._guard, "verify",
                          side_effect=PermissionError("simulated lock")):
            with self.assertRaises(PermissionError):
                self.authority.wait_local()
        self.assertEqual(self.authority.wait_local(5)["status"], "COMPLETED")

    def test_independent_windows_process_owns_gui_and_job(self):
        """Separate process owns Host, approval records, GUI and JobManager."""
        code = (
            "import sys,tempfile,secrets;"
            "from unittest.mock import patch;"
            "from sora_commander_next.commands import Command;"
            "from pathlib import Path;"
            "from sora_commander_next.offline_owner_authority import DevOwnerAuthority;"
            "fixture=Path(sys.argv[1]);"
            "tmp=tempfile.TemporaryDirectory(prefix='sora-owner-child-');"
            "req=Command(sys.executable,('-I','-S','-c',\"print('child-owner-ok')\"),"
            "tmp.name,timeout=4,output_limit=2048,request_id=secrets.token_hex(16));"
            "broker=DevOwnerAuthority(fixed_command=req);"
            "pending=broker.stage_local();"
            "ctx=patch('sora_commander_next.owner_approval_broker.DIALOG_SCRIPT',fixture);"
            "ctx.start();"
            "decision=broker.review_once_local();"
            "ctx.stop();"
            "result=broker.wait_local(5) if decision=='RUNNING' else None;"
            "print('LOCAL_OWNER_GATE',decision,"
            "result['status'] if result else 'NO_JOB',"
            "result['exit_code'] if result else 'NONE',"
            "broker._jobs.list_sessions()['count'],flush=True);"
            "broker.close();tmp.cleanup()"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = (str(ROOT / "src")
                                  + os.pathsep + env.get("PYTHONPATH", ""))
        for fixture, expected in (
                (APPROVE, "LOCAL_OWNER_GATE RUNNING COMPLETED 0 1"),
                (DENY, "LOCAL_OWNER_GATE DENIED NO_JOB NONE 0")):
            with self.subTest(fixture=fixture.name):
                result = subprocess.run(
                    [sys.executable, "-c", code, str(fixture)],
                    capture_output=True, text=True, timeout=12, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
