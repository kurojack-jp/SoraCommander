"""Phase 9B real Windows anonymous-pipe child / GUI protocol tests.

The APPROVE/DENY child fixtures are SYNTHETIC, not a human-click acceptance.
Actual Tk dialogs are never opened by the automated test suite.
"""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.approval import LocalApproval
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.windows_owner import OwnerSessionGuard, current_windows_owner
from sora_commander_next.windows_owner_controller import WindowsOwnerController
from sora_commander_next.owner_approval_broker import OwnerApprovalBroker
from sora_commander_next.owner_approval_dialog import parse_request

FIXTURES=ROOT/"tests/fixtures"


class Phase9BIPCTests(unittest.TestCase):
    def setUp(self):
        self.now=1000.0
        self.identity=current_windows_owner()
        self.host=Host(Policy((str(ROOT),),clock=lambda:self.now),
            self.identity.sid, LocalApproval(lambda:self.now))
        self.addCleanup(self.host.jobs.close)
        self.controller=WindowsOwnerController(self.host)
        self.broker=OwnerApprovalBroker(self.controller)

    def pending(self, code="print('phase9-approval')"):
        command=Command(sys.executable,("-I","-S","-c",code),str(ROOT))
        result=self.host.run_command(background=True,
            executable=command.executable,argv=list(command.argv),cwd=command.cwd)
        self.assertEqual(result["status"],"REVIEW")
        return result["pending_id"]

    def test_os_process_identity_matches_exact_owner_logon(self):
        self.assertTrue(OwnerSessionGuard().verify_process(os.getpid()))
        with self.assertRaises(PermissionError):
            OwnerSessionGuard().verify_process(-1)

    def test_synthetic_private_pipe_approve_once(self):
        pending=self.pending()
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT",
                   FIXTURES/"phase9_dialog_approve.py"):
            result=self.broker.review_once(pending)
        self.assertEqual(result["status"],"RUNNING",result)
        self.assertEqual(result["decision"],"REVIEW")
        self.assertEqual(self.host.jobs.wait(result["session_id"])["stdout"].strip(),
                         "phase9-approval")
        self.assertEqual(self.broker.review_once(pending)["status"],"ERROR")

    def test_synthetic_private_pipe_reject_does_not_spawn(self):
        pending=self.pending()
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT",
                   FIXTURES/"phase9_dialog_deny.py"):
            result=self.broker.review_once(pending)
        self.assertEqual(result["status"],"DENIED",result)
        self.assertEqual(len(self.host.jobs._jobs),0)
        self.assertEqual(self.controller.reject_local(pending)["status"],"ERROR")
        self.assertEqual(self.controller.approve_local(pending)["status"],"ERROR")
        self.assertFalse(any(p.pending_id==pending for p in self.controller.pending_local()))

    def test_incorrect_challenge_fails_closed_without_running_command(self):
        pending=self.pending()
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT",
                   FIXTURES/"phase9_dialog_bad.py"):
            result=self.broker.review_once(pending)
        self.assertEqual(result["status"],"ERROR",result)
        self.assertEqual(result["error"],"OwnerConfirmationUnavailable")
        self.assertFalse(self.host.jobs._jobs)
        self.assertEqual(self.controller.approve_local(pending)["status"],"ERROR")
        self.assertFalse(any(p.pending_id==pending for p in self.controller.pending_local()))

    def test_child_identity_mismatch_must_not_honor_approval(self):
        pending=self.pending()
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT",
                   FIXTURES/"phase9_dialog_approve.py"), patch.object(
                       self.controller._guard,"verify_process",
                       side_effect=PermissionError("wrong session")):
            result=self.broker.review_once(pending)
        self.assertEqual(result["status"],"ERROR")
        self.assertFalse(self.host.jobs._jobs)
        self.assertNotIn("wrong session",str(result))

    def test_expiry_and_missing_pending_cannot_launch_gui(self):
        pending=self.pending()
        self.now+=61
        with patch("sora_commander_next.owner_approval_broker.subprocess.Popen") as p:
            self.assertEqual(self.broker.review_once(pending)["status"],"ERROR")
            self.assertEqual(self.broker.review_once("not-a-pending-id")["status"],"ERROR")
        p.assert_not_called()

    def test_dialog_parse_refuses_bad_commands_without_tk_import(self):
        for bad in (b"",b"{}\n",b"not-json\n",b"{}",
                    b" "*(131073)+b"\n"):
            with self.subTest(length=len(bad)),self.assertRaises(ValueError):
                parse_request(bad)
        self.assertNotIn("tkinter",sys.modules)

    def test_no_automatic_dialog_and_no_remote_approval_registration(self):
        self.assertFalse(hasattr(self.host,"approve_local"))
        self.assertFalse(hasattr(self.host,"reject_local"))
        self.assertEqual(self.host.ping()["status"],"OK")
        self.assertFalse(self.host.jobs._jobs)


if __name__=="__main__":
    unittest.main()
