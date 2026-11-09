"""Phase 9D actual Win32 WTS listener + synthetic session notifications.

No real Windows lock, desktop GUI, external listener, service, or Task.
Uses PostMessage(WM_WTSSESSION_CHANGE) exclusively to its owned hidden HWND.
"""
from pathlib import Path
import sys
from threading import Thread
from time import monotonic, sleep
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.windows_owner import current_windows_owner
from sora_commander_next.windows_session_events import (
    OwnerSessionEventLatch, WM_WTSSESSION_CHANGE, post_message)
from sora_commander_next.windows_owner_controller import WindowsOwnerController
from sora_commander_next.owner_approval_broker import OwnerApprovalBroker
from sora_commander_next.approval import LocalApproval
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy


def until(predicate, seconds=3.0):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.01)
    raise AssertionError("expected Win32 event was not delivered")


class Phase9DEventLatchTests(unittest.TestCase):
    def setUp(self):
        self.identity = current_windows_owner()
        self.listener = OwnerSessionEventLatch(self.identity.session_id)
        self.addCleanup(self.listener.close)

    def send(self, latch, kind, sid=None):
        self.assertTrue(latch._hwnd, "hidden WTS listener window missing")
        self.assertTrue(post_message(latch._hwnd, WM_WTSSESSION_CHANGE, kind,
                                     sid if sid is not None else self.identity.session_id),
                        "Win32 PostMessage failed")

    def test_listener_can_arm_and_close_without_a_visible_window(self):
        self.listener.start()
        self.assertTrue(self.listener.verify())
        self.assertFalse(self.listener.invalidated())
        self.listener.close()
        self.assertTrue(self.listener.invalidated())
        with self.assertRaises(PermissionError):
            self.listener.verify()
        self.listener.close()

    def test_synthetic_lock_then_unlock_can_never_rearm_review(self):
        self.listener.start()
        self.send(self.listener, 0x7)  # WTS_SESSION_LOCK; NOT an OS lock
        until(self.listener.invalidated)
        self.assertTrue(self.listener.saw_lock_event())
        self.send(self.listener, 0x8)  # WTS_SESSION_UNLOCK
        with self.assertRaises(PermissionError):
            self.listener.verify()
        self.assertTrue(self.listener.invalidated())

    def test_synthetic_disconnect_logoff_and_reconnect_all_invalidate(self):
        for kind in (0x2, 0x4, 0x6, 0x1, 0x3, 0x8):
            latch = OwnerSessionEventLatch(self.identity.session_id).start()
            try:
                self.send(latch, kind)
                until(latch.invalidated)
                with self.assertRaises(PermissionError):
                    latch.verify()
            finally:
                latch.close()

    def test_unrelated_session_message_does_not_invalidate_owner(self):
        self.listener.start()
        self.send(self.listener, 0x7, self.identity.session_id + 123)
        sleep(.12)
        self.assertTrue(self.listener.verify())
        self.assertFalse(self.listener.invalidated())

    def test_invalid_session_registration_and_reuse_fail_closed(self):
        for invalid in (0, -1, "1", None, True, 0xFFFFFFFF):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                OwnerSessionEventLatch(invalid)
        self.listener.start()
        with self.assertRaises(RuntimeError):
            self.listener.start()
        self.listener.close()
        with self.assertRaises(RuntimeError):
            self.listener.start()

    def test_registration_failure_denies_and_leaves_no_working_listener(self):
        with patch("sora_commander_next.windows_session_events.wts_register",return_value=False):
            with self.assertRaises(PermissionError):
                self.listener.start()
        self.assertTrue(self.listener.invalidated())
        with self.assertRaises(PermissionError):
            self.listener.verify()


class Phase9DBrokerTests(unittest.TestCase):
    def test_fake_lock_event_while_gui_awaits_reply_never_executes(self):
        identity = current_windows_owner()
        host = Host(Policy((str(ROOT),)), identity.sid, LocalApproval())
        self.addCleanup(host.jobs.close)
        owner = WindowsOwnerController(host)
        broker = OwnerApprovalBroker(owner)
        req = Command(sys.executable, ("-I", "-S", "-c", "print('NEVER_RUN')"), str(ROOT))
        response = host.run_command(background=True, executable=req.executable,
                                    argv=list(req.argv), cwd=req.cwd)
        self.assertEqual(response["status"], "REVIEW")
        observed = []
        actual_start = OwnerSessionEventLatch.start

        def intercept_start(instance):
            result = actual_start(instance)
            observed.append(instance)
            return result

        completed = []
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT",
                   ROOT / "tests/fixtures/phase9_dialog_slow_approve.py"), patch.object(
                   OwnerSessionEventLatch,"start", intercept_start):
            thread = Thread(target=lambda: completed.append(
                broker.review_once(response["pending_id"])),daemon=True)
            thread.start()
            try:
                until(lambda: bool(observed) and bool(observed[0]._hwnd))
                latch = observed[0]
                self.assertTrue(post_message(latch._hwnd, WM_WTSSESSION_CHANGE,
                                             0x7, identity.session_id))
                until(latch.invalidated)
                thread.join(timeout=4)
                self.assertFalse(thread.is_alive(),"blocked Owner dialog did not abort")
            finally:
                thread.join(timeout=4)
        self.assertEqual(completed, [{"status":"ERROR","error":"OwnerConfirmationUnavailable",
                                      "stage":"FIRST_GUI", "reason":"CONFIRMATION_UNAVAILABLE",
                                      "approval_state":"NOT_CONSUMED", "launch_outcome":"NOT_STARTED"}])
        self.assertFalse(host.jobs._jobs)
        # After simulated unlock, a fresh local controller must not be
        # able to approve a REVIEW invalidated by the earlier lock.
        self.assertEqual(owner.approve_local(response["pending_id"])["status"],"ERROR")
        self.assertFalse(any(p.pending_id==response["pending_id"]
                             for p in owner.pending_local()))
        self.assertEqual(host.ping()["status"],"OK")

    def test_controller_cannot_start_with_an_invalidated_real_latch(self):
        identity=current_windows_owner()
        host=Host(Policy((str(ROOT),)),identity.sid,LocalApproval())
        self.addCleanup(host.jobs.close)
        owner=WindowsOwnerController(host)
        req=Command(sys.executable,("-I","-S","-c","print('NEVER')"),str(ROOT))
        pending=host.run_command(background=True,executable=req.executable,
                                 argv=list(req.argv),cwd=req.cwd)["pending_id"]
        latch=OwnerSessionEventLatch(identity.session_id).start()
        try:
            self.assertTrue(post_message(latch._hwnd,WM_WTSSESSION_CHANGE,0x7,
                                         identity.session_id))
            until(latch.invalidated)
            result=owner.approve_local(pending,session_latch=latch)
            self.assertEqual(result["status"],"ERROR")
            self.assertFalse(host.jobs._jobs)
        finally:
            latch.close()


if __name__=="__main__":
    unittest.main()
