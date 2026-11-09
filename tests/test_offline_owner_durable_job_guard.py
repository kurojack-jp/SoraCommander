"""DEV-only real Windows Owner GUI path + SQLite launch replay evidence.

Synthetic GUI fixtures stand in for a click; NO assertion of live human
approval, installed Broker, protected SQLite, IPC or deployment. Fixed benign
Python command only. The SQLite row can DENY repeats but never grant launch.
"""
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sora_commander_next.approval_v2_ledger import OfflineApprovalLedger
from sora_commander_next.commands import Command
from sora_commander_next.offline_owner_authority import DevOwnerAuthority

ROOT = Path(__file__).resolve().parents[1]
APPROVE = ROOT / "tests" / "fixtures" / "phase9_dialog_approve.py"
DENY = ROOT / "tests" / "fixtures" / "phase9_dialog_deny.py"


@unittest.skipUnless(os.name == "nt", "Owner Windows GUI/Job acceptance")
class OwnerDurableLaunchGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="sora-owner-fence-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "launch.sqlite3"
        self.request = Command(
            sys.executable,
            ("-I", "-S", "-c", "print('durable-once')"),
            str(self.root), timeout=4, output_limit=2048,
            request_id=secrets.token_hex(16))
        self.ledger = OfflineApprovalLedger(self.path)
        self.addCleanup(self.ledger.close)

    def authority(self, request=None, ledger=None):
        authority = DevOwnerAuthority(
            fixed_command=request or self.request,
            offline_ledger=ledger or self.ledger)
        self.addCleanup(authority.close)
        return authority

    def test_verified_synthetic_approve_spawns_one_and_persists_terminal(self):
        owner = self.authority()
        owner.stage_local()
        self.assertEqual(self.ledger.inspect_launch_attempt_data_only(
            self.request).status, "NOT_ATTEMPTED_DATA_ONLY")
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT", APPROVE):
            self.assertEqual(owner.review_once_local(), "RUNNING")
        result = owner.wait_local(5)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["stdout"].strip(), "durable-once")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(owner._jobs.list_sessions()["count"], 1)
        self.assertEqual(self.ledger.inspect_launch_attempt_data_only(
            self.request).status, "ALREADY_ATTEMPTED_TERMINAL")
        self.assertEqual(self.ledger.list_claims_data_only(
            owner._guard.sid), ())  # No data-only consent issued as authority
        self.assertEqual(self.ledger.audit_count(), 2)
        self.ledger.close()
        reopened = OfflineApprovalLedger(self.path)
        self.addCleanup(reopened.close)
        replay = self.authority(ledger=reopened)
        with self.assertRaises(PermissionError):
            replay.stage_local()  # no second GUI
        self.assertEqual(replay._jobs.list_sessions()["count"], 0)

    def test_owner_denial_never_reserves_attempt_or_spawns(self):
        owner = self.authority()
        owner.stage_local()
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT", DENY):
            self.assertEqual(owner.review_once_local(), "DENIED")
        self.assertEqual(owner._jobs.list_sessions()["count"], 0)
        self.assertEqual(self.ledger.inspect_launch_attempt_data_only(
            self.request).status, "NOT_ATTEMPTED_DATA_ONLY")
        self.assertEqual(self.ledger.audit_count(), 0)

    def test_os_launch_error_keeps_unknown_after_ledger_reopen(self):
        owner = self.authority()
        owner.stage_local()
        with (patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT", APPROVE),
              patch.object(owner._jobs, "start",
                           side_effect=OSError("synthetic ambiguous launch")) as spawn):
            self.assertEqual(owner.review_once_local(), "OUTCOME_UNKNOWN")
            self.assertEqual(spawn.call_count, 1)
        self.assertEqual(self.ledger.inspect_launch_attempt_data_only(
            self.request).status, "ALREADY_ATTEMPTED_UNKNOWN")
        self.ledger.close()
        reopened = OfflineApprovalLedger(self.path)
        self.addCleanup(reopened.close)
        attempt = self.authority(ledger=reopened)
        with self.assertRaises(PermissionError):
            attempt.stage_local()
        self.assertEqual(attempt._jobs.list_sessions()["count"], 0)

    def test_sqlite_failure_cannot_launch(self):
        owner = self.authority()
        owner.stage_local()
        with (patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT", APPROVE),
              patch.object(self.ledger, "reserve_launch_attempt_data_only",
                           side_effect=OSError("disk failed")),
              patch.object(owner._jobs, "start",
                           side_effect=AssertionError("job must not spawn")) as spawn):
            self.assertEqual(owner.review_once_local(), "OUTCOME_UNKNOWN")
            spawn.assert_not_called()
        self.assertEqual(self.ledger.inspect_launch_attempt_data_only(
            self.request).status, "NOT_ATTEMPTED_DATA_ONLY")

    def test_cross_process_second_attempt_never_opens_gui_or_spawns(self):
        code = """
import sys
from pathlib import Path
from unittest.mock import patch
from sora_commander_next.commands import Command
from sora_commander_next.approval_v2_ledger import OfflineApprovalLedger
from sora_commander_next.offline_owner_authority import DevOwnerAuthority
db, cwd, rid, fixture = sys.argv[1:]
req = Command(sys.executable, ('-I', '-S', '-c', "print('durable-once')"),
              cwd, timeout=4, output_limit=2048, request_id=rid)
ledger = OfflineApprovalLedger(Path(db))
broker = DevOwnerAuthority(fixed_command=req, offline_ledger=ledger)
try:
    try:
        broker.stage_local()
    except PermissionError:
        print('DEV_RESULT REPLAY_DENIED NO_JOB', flush=True)
    else:
        with patch('sora_commander_next.owner_approval_broker.DIALOG_SCRIPT',
                   Path(fixture)):
            decision = broker.review_once_local()
        result = broker.wait_local(5) if decision == 'RUNNING' else None
        print('DEV_RESULT', decision,
              result['status'] if result else 'NO_JOB', flush=True)
finally:
    broker.close()
    ledger.close()
"""
        # Run one isolated Owner-process with its own real Windows JobManager;
        # the second process uses the same fixed request ID but must not launch.
        env = dict(os.environ)
        env["PYTHONPATH"] = (str(ROOT / "src") + os.pathsep
                             + env.get("PYTHONPATH", ""))
        self.ledger.close()
        outcomes = []
        for i in range(2):
            proc = subprocess.run(
                [sys.executable, "-c", code, str(self.path), str(self.root),
                 self.request.request_id, str(APPROVE)],
                capture_output=True, text=True, env=env, timeout=18)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outcomes.append(proc.stdout.strip())
        self.assertEqual(outcomes[0], "DEV_RESULT RUNNING COMPLETED")
        self.assertEqual(outcomes[1], "DEV_RESULT REPLAY_DENIED NO_JOB")
        reopened = OfflineApprovalLedger(self.path)
        try:
            self.assertEqual(reopened.inspect_launch_attempt_data_only(
                self.request).status, "ALREADY_ATTEMPTED_TERMINAL")
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
