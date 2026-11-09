"""Inert SQLite launch-attempt receipts: no GUI, grants or OS commands.

Confirms the same single-use attempt key survives SQLite reconnect, competing
handles, and simulated audit failure. Never run a client-supplied command.
"""
from dataclasses import replace
from pathlib import Path
import secrets
import sys
import tempfile
from threading import Barrier, Thread, Lock
import unittest
from unittest.mock import patch

from sora_commander_next.approval_v2_ledger import OfflineApprovalLedger
from sora_commander_next.commands import Command


class LaunchAttemptDataOnlyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="sora-launch-id-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "launch.sqlite3"
        self.db = OfflineApprovalLedger(self.path)
        self.addCleanup(self.db.close)
        self.command = Command(
            sys.executable, ("-I", "-S", "-c", "print('inert')"),
            str(self.root), timeout=4, output_limit=4096,
            request_id=secrets.token_hex(16))

    def test_attempt_recorded_then_reopen_remains_irreversible(self):
        receipt = self.db.reserve_launch_attempt_data_only(self.command)
        self.assertEqual(receipt.status, "ATTEMPT_RECORDED_DATA_ONLY")
        self.db.close()
        self.db = OfflineApprovalLedger(self.path)
        self.addCleanup(self.db.close)
        repeated = self.db.reserve_launch_attempt_data_only(self.command)
        self.assertEqual(repeated.status, "ALREADY_ATTEMPTED_UNKNOWN")
        self.assertEqual(self.db.conclude_launch_attempt_data_only(
            self.command).status, "ATTEMPT_TERMINAL_DATA_ONLY")
        self.assertEqual(self.db.conclude_launch_attempt_data_only(
            self.command).status, "ALREADY_ATTEMPTED_TERMINAL")
        self.assertEqual(self.db.reserve_launch_attempt_data_only(
            self.command).status, "ALREADY_ATTEMPTED_TERMINAL")
        self.assertEqual(self.db.audit_count(), 2)

    def test_request_id_conflict_protected_even_after_terminal(self):
        self.db.reserve_launch_attempt_data_only(self.command)
        modified = (
            replace(self.command, argv=("--version",)),
            replace(self.command, timeout=2),
            replace(self.command, output_limit=1024),
            replace(self.command, cwd=str(self.root.parent)),
        )
        for value in modified:
            with self.subTest(changed=value):
                self.assertEqual(self.db.reserve_launch_attempt_data_only(
                    value).status, "DENY_DUPLICATE_CONFLICT")
        self.db.conclude_launch_attempt_data_only(self.command)
        self.assertEqual(self.db.inspect_launch_attempt_data_only(
            modified[0]).status, "DENY_DUPLICATE_CONFLICT")
        self.assertEqual(self.db.audit_count(), 2)

    def test_two_sqlite_instances_one_winner_and_no_implicit_claim(self):
        other = OfflineApprovalLedger(self.path)
        self.addCleanup(other.close)
        barrier, lock = Barrier(3), Lock()
        results, errors = [], []
        def reserve(db):
            barrier.wait()
            try:
                out = db.reserve_launch_attempt_data_only(self.command)
                with lock:
                    results.append(out.status)
            except Exception as e:
                with lock:
                    errors.append(type(e).__name__)
        threads = [Thread(target=reserve, args=(db,))
                   for db in (self.db, other)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=8)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertCountEqual(results, [
            "ATTEMPT_RECORDED_DATA_ONLY", "ALREADY_ATTEMPTED_UNKNOWN"])
        self.assertEqual(self.db.audit_count(), 1)
        # The new table only records a replay deny marker. It cannot generate
        # a matching v2 grant or Owner approval.
        self.assertEqual(self.db.inspect_launch_attempt_data_only(
            self.command).status, "ALREADY_ATTEMPTED_UNKNOWN")

    def test_failed_audit_rolls_back_and_does_not_create_positive_authority(self):
        with patch.object(self.db, "_record",
                          side_effect=OSError("simulated sqlite audit failure")):
            with self.assertRaises(OSError):
                self.db.reserve_launch_attempt_data_only(self.command)
        self.assertEqual(self.db.inspect_launch_attempt_data_only(
            self.command).status, "NOT_ATTEMPTED_DATA_ONLY")
        self.assertEqual(self.db.audit_count(), 0)
        self.assertEqual(self.db.conclude_launch_attempt_data_only(
            self.command).status, "NOT_FOUND")
        self.assertEqual(self.db.reserve_launch_attempt_data_only(
            self.command).status, "ATTEMPT_RECORDED_DATA_ONLY")

    def test_wrong_type_and_invalid_id_never_create_receipt(self):
        for invalid in ("not a command", {}, None,
                        replace(self.command, request_id="bad")):
            with self.subTest(value=invalid), self.assertRaises((TypeError, ValueError)):
                self.db.reserve_launch_attempt_data_only(invalid)
        self.assertEqual(self.db.audit_count(), 0)


if __name__ == "__main__":
    unittest.main()
