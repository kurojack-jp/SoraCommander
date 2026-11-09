"""Terra P1: Owner session must be rechecked after CLI monitor startup.

Mock-only tests. Never spawn an approved command or change Windows session.
"""
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from sora_commander_next.approval import LocalApproval
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.windows_owner import WindowsOwnerIdentity
from sora_commander_next.windows_owner_controller import WindowsOwnerController

ROOT = Path(__file__).resolve().parents[1]


class OwnerLockDuringMonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.valid = WindowsOwnerIdentity("S-1-5-21-99-1001", 1, (12, 5), True)
        self.current = self.valid
        probe = patch("sora_commander_next.windows_owner.current_windows_owner",
                      side_effect=lambda: self.current)
        probe.start()
        self.addCleanup(probe.stop)
        self.host = Host(Policy((str(ROOT),), clock=lambda: self.now),
                         self.valid.sid, LocalApproval(lambda: self.now))
        self.addCleanup(self.host.jobs.close)
        self.ctrl = WindowsOwnerController(self.host)

    def pending(self):
        cmd = Command(sys.executable, ("-I", "-S", "-c", "print('no real launch')"),
                      str(ROOT))
        result = self.host.run_command(background=True, executable=cmd.executable,
                                       argv=list(cmd.argv), cwd=cmd.cwd)
        self.assertEqual(result["status"], "REVIEW")
        return result["pending_id"]

    def check_failure(self, monitor_callback):
        pending_id = self.pending()
        with (patch("sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job",
                    side_effect=monitor_callback) as monitor,
              patch.object(self.host.jobs, "start") as spawn):
            result = self.ctrl.approve_local(pending_id)
        self.assertEqual(result, {"status": "ERROR", "error": "OwnerAuthorizationRequired",
                                 "stage": "MONITOR", "reason": "AUTHORIZATION_FAILED",
                                 "approval_state": "CONSUMED", "launch_outcome": "NOT_STARTED"})
        monitor.assert_called_once()
        spawn.assert_not_called()
        # Approval was already consumed, not replayable after session unlock.
        self.current = self.valid
        with patch.object(self.host.jobs, "start") as spawn:
            self.assertEqual(self.ctrl.approve_local(pending_id)["status"], "ERROR")
            spawn.assert_not_called()

    def test_lock_during_monitor_never_starts_job(self):
        self.check_failure(lambda _: setattr(
            self, "current", replace(self.valid, active=False)))

    def test_logon_luid_change_during_monitor_never_starts_job(self):
        self.check_failure(lambda _: setattr(
            self, "current", replace(self.valid, logon_luid=(13, 5))))

    def test_session_switch_during_monitor_never_starts_job(self):
        self.check_failure(lambda _: setattr(
            self, "current", replace(self.valid, session_id=3)))

    def test_approval_expiry_during_monitor_never_starts_job(self):
        self.check_failure(lambda _: setattr(self, "now", 200.0))


if __name__ == "__main__":
    unittest.main()
