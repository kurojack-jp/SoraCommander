"""Phase 9C lock/disconnect fail-closed, real WTS query, synthetic GUI wait.

No workstation lock, GUI launch, public MCP endpoint or Task modification.
"""
import ctypes
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))

from sora_commander_next.windows_session_lock import (
    decode_session_flags,session_is_unlocked,_Info)
from sora_commander_next.windows_owner import (
    OwnerSessionGuard,current_windows_owner,WindowsOwnerIdentity)
from sora_commander_next.windows_owner_controller import WindowsOwnerController
from sora_commander_next.approval import LocalApproval
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.commands import Command
from sora_commander_next.owner_approval_broker import OwnerApprovalBroker


class Phase9CLockTests(unittest.TestCase):
    def test_real_windows_desktop_state_reported_unlocked(self):
        real=current_windows_owner()
        self.assertTrue(real.active)
        self.assertTrue(session_is_unlocked(real.session_id))
        self.assertTrue(OwnerSessionGuard().verify())

    def test_raw_session_ex_flags_and_connection_must_match(self):
        session=current_windows_owner().session_id
        def encode(flags=1,state=0,sid=session,level=1):
            x=_Info()
            x.level=level
            x.data.head.session_id=sid
            x.data.head.connection_state=state
            x.data.head.flags=flags
            return bytes(x)
        self.assertTrue(decode_session_flags(encode(),session))
        for blob in (encode(flags=0), encode(flags=-1),
                     encode(flags=2),encode(state=4)):
            self.assertFalse(decode_session_flags(blob,session))
        for blob in (encode(sid=session+1),encode(level=2),
                     encode()[:10],b"",None):
            with self.subTest(blob=repr(blob)[:12]),self.assertRaises(PermissionError):
                decode_session_flags(blob,session)
        with self.assertRaises(PermissionError):
            decode_session_flags(encode(),0)

    def test_guard_denies_mock_lock_disconnection_and_unknown(self):
        identity=current_windows_owner()
        with patch("sora_commander_next.windows_owner.session_is_unlocked",return_value=True) as check:
            guard=OwnerSessionGuard()
            self.assertTrue(guard.verify())
            self.assertGreaterEqual(check.call_count,2)
        with patch("sora_commander_next.windows_owner.session_is_unlocked",return_value=False):
            with self.assertRaises(PermissionError):
                OwnerSessionGuard()
            with self.assertRaises(PermissionError):
                guard.verify()
        with patch("sora_commander_next.windows_owner.session_is_unlocked",side_effect=OSError):
            with self.assertRaises(PermissionError):
                guard.verify()
        with patch("sora_commander_next.windows_owner.current_windows_owner",
                   return_value=WindowsOwnerIdentity(identity.sid,identity.session_id,
                                                       identity.logon_luid,False)):
            with self.assertRaises(PermissionError):
                guard.verify()

    def test_lock_during_dialog_aborts_before_approve(self):
        real=current_windows_owner()
        host=Host(Policy((str(ROOT),)),real.sid,LocalApproval())
        self.addCleanup(host.jobs.close)
        ctrl=WindowsOwnerController(host)
        broker=OwnerApprovalBroker(ctrl)
        cmd=Command(sys.executable,("-I","-S","-c","print('must-not-run')"),str(ROOT))
        pending=host.run_command(background=True,executable=cmd.executable,
              argv=list(cmd.argv),cwd=cmd.cwd)["pending_id"]
        # Observe initial owner/unlocked state, then simulate lock while
        # child is awaiting its delayed yes. This never locks real Windows.
        calls=[]
        original=ctrl._guard.verify
        def lock_after_start():
            calls.append(1)
            if len(calls)>4:
                raise PermissionError("simulated Owner desktop locked")
            return original()
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT",
                   ROOT/"tests/fixtures/phase9_dialog_slow_approve.py"), patch.object(
                   ctrl._guard,"verify",side_effect=lock_after_start):
            result=broker.review_once(pending)
        self.assertEqual(result["status"],"ERROR")
        self.assertEqual(result["error"],"OwnerConfirmationUnavailable")
        self.assertFalse(host.jobs._jobs)
        self.assertTrue(len(calls)>4)
        self.assertEqual(host.ping()["status"],"OK")

    def test_lock_at_return_does_not_approve(self):
        real=current_windows_owner()
        host=Host(Policy((str(ROOT),)),real.sid,LocalApproval())
        self.addCleanup(host.jobs.close)
        ctrl=WindowsOwnerController(host)
        broker=OwnerApprovalBroker(ctrl)
        command=Command(sys.executable,("-I","-S","-c","print('blocked')"),str(ROOT))
        pending=host.run_command(background=True,executable=command.executable,
                                argv=list(command.argv),cwd=command.cwd)["pending_id"]
        # Simulate an OS lock transition at the exact final approval
        # boundary, after a valid synthetic UI response arrives.
        state={"unlocked":True}
        original_approve=ctrl.approve_local
        def lock_before_consuming(pending_id):
            state["unlocked"]=False
            return original_approve(pending_id)
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT",
                   ROOT/"tests/fixtures/phase9_dialog_approve.py"), patch(
                   "sora_commander_next.windows_owner.session_is_unlocked",
                   side_effect=lambda sid:state["unlocked"]), patch.object(
                   ctrl,"approve_local",side_effect=lock_before_consuming):
            result=broker.review_once(pending)
        # Even a completed UI response cannot authorize a locked session.
        self.assertNotEqual(result["status"],"RUNNING")
        self.assertFalse(host.jobs._jobs)


if __name__=="__main__":
    unittest.main()
