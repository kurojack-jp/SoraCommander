"""Phase 9 Windows SID + logon LUID + active-session approval guard tests.

Stdlib-only; no UI launches, Task changes, MCP exposure or remote approval.
"""
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.approval import LocalApproval
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.windows_owner import (WindowsOwnerIdentity,
    OwnerSessionGuard, current_windows_owner)
from sora_commander_next.windows_owner_controller import WindowsOwnerController


class Phase9OwnerTests(unittest.TestCase):
    def setUp(self):
        self.now=10.0
        self.valid=WindowsOwnerIdentity("S-1-5-21-99-1001",1,(12,5),True)
        self.current=self.valid
        self.owner_probe=patch("sora_commander_next.windows_owner.current_windows_owner",side_effect=lambda:self.current)
        self.owner_mock=self.owner_probe.start()
        self.addCleanup(self.owner_probe.stop)
        self.guard=OwnerSessionGuard()
        self.host=Host(Policy((str(ROOT),),clock=lambda:self.now),
                       self.valid.sid, LocalApproval(lambda:self.now))
        self.addCleanup(self.host.jobs.close)
        self.ctrl=WindowsOwnerController(self.host)

    def pending(self, code="print('approved')"):
        cmd=Command(sys.executable,("-I","-S","-c",code),str(ROOT))
        result=self.host.run_command(background=True,executable=cmd.executable,
            argv=list(cmd.argv),cwd=cmd.cwd)
        self.assertEqual(result["status"],"REVIEW")
        return result["pending_id"]

    def test_windows_identity_is_real_and_active_on_connected_desktop(self):
        identity=current_windows_owner()
        self.assertTrue(identity.sid.startswith("S-1-5-21-"))
        self.assertGreater(identity.session_id,0)
        self.assertNotEqual(identity.logon_luid,(0,0))
        self.assertTrue(identity.active)
        with patch("sora_commander_next.windows_owner.current_windows_owner",new=current_windows_owner):
            guard=OwnerSessionGuard()
            self.assertTrue(guard.verify())
        self.assertEqual(guard.sid,identity.sid)

    def test_owner_recheck_rejects_other_user_session_and_logon(self):
        cases=(
            replace(self.valid,sid="S-1-5-21-99-2002"),
            replace(self.valid,session_id=2),
            replace(self.valid,logon_luid=(13,5)),
            replace(self.valid,active=False)
        )
        for identity in cases:
            with self.subTest(identity=identity):
                pending=self.pending()
                self.current=identity
                self.assertEqual(self.ctrl.pending_local(),())
                self.assertEqual(self.ctrl.approve_local(pending),
                                 {"status":"ERROR","error":"OwnerAuthorizationRequired",
                                  "stage":"OWNER_CHECK", "reason":"AUTHORIZATION_FAILED",
                                  "approval_state":"NOT_CONSUMED", "launch_outcome":"NOT_STARTED"})
                self.assertEqual(self.ctrl.reject_local(pending)["status"],"ERROR")
                self.assertEqual(len(self.host.jobs._jobs),0)
                self.current=self.valid
                self.assertEqual(self.ctrl.reject_local(pending)["status"],"REJECTED")
                self.assertEqual(self.host.ping()["status"],"OK")

    def test_owner_sid_renamed_after_creation_fails_closed(self):
        pending=self.pending()
        self.host.owner="forged-label"
        self.assertEqual(self.ctrl.approve_local(pending)["status"],"ERROR")
        self.assertEqual(len(self.host.jobs._jobs),0)
        self.host.owner=self.valid.sid
        self.assertEqual(self.ctrl.reject_local(pending)["status"],"REJECTED")

    def test_approval_runs_once_only_when_same_windows_logon(self):
        pending=self.pending()
        first=self.ctrl.approve_local(pending)
        self.assertEqual(first["decision"],"REVIEW")
        self.assertEqual(first["status"],"RUNNING")
        self.assertEqual(self.ctrl.approve_local(pending)["status"],"ERROR")
        done=self.host.jobs.wait(first["session_id"])
        self.assertEqual(done["status"],"COMPLETED")
        self.assertEqual(done["stdout"].strip(),"approved")
        self.assertEqual(self.host.ping()["status"],"OK")

    def test_pending_context_mutation_and_expiry_never_execute(self):
        pending=self.pending()
        self.host.policy.generation+=1
        self.assertEqual(self.ctrl.approve_local(pending)["status"],"ERROR")
        self.assertEqual(len(self.host.jobs._jobs),0)
        pending=self.pending()
        self.now+=61
        self.assertEqual(self.ctrl.approve_local(pending)["status"],"ERROR")
        self.assertEqual(len(self.host.jobs._jobs),0)

    def test_unavailable_identity_reader_never_approves(self):
        pending=self.pending()
        self.owner_mock.side_effect=OSError("sensitive path")
        result=self.ctrl.approve_local(pending)
        self.assertEqual(result,{"status":"ERROR","error":"OwnerAuthorizationRequired",
                                "stage":"OWNER_CHECK", "reason":"AUTHORIZATION_FAILED",
                                "approval_state":"NOT_CONSUMED", "launch_outcome":"NOT_STARTED"})
        self.assertNotIn("sensitive",str(result))
        self.assertFalse(self.host.jobs._jobs)

    def test_stale_or_inactive_identity_cannot_initialize(self):
        for identity in (replace(self.valid,active=False),
                         "caller supplied SID"):
            with self.assertRaises((PermissionError, TypeError)):
                with patch("sora_commander_next.windows_owner.current_windows_owner",return_value=identity):
                    WindowsOwnerController(self.host)

    def test_wrong_host_owner_rejected_on_initialization(self):
        self.host.owner="local-owner"
        with self.assertRaises(PermissionError):
            WindowsOwnerController(self.host)
        self.assertFalse(self.host.jobs._jobs)

    def test_mcp_host_still_has_no_remote_approval_method(self):
        self.assertFalse(hasattr(self.host,"approve_local"))
        self.assertFalse(hasattr(self.host,"reject_local"))
        self.assertEqual(self.host.status()["approval"],"LOCAL_DEMO_ONLY")


if __name__=="__main__":
    unittest.main()
