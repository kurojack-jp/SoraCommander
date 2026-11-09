"""Real disposable Windows Owner-verified job -> bounded v2 read-only pages.

A synthetic dialog response is NOT a real human click. There is no named
pipe, MCP endpoint or installed protected Broker in this acceptance.
"""
import base64
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch

from sora_commander_next.approval_v2_ledger import OfflineApprovalLedger
from sora_commander_next.commands import Command
from sora_commander_next.offline_owner_authority import DevOwnerAuthority
from sora_commander_next.offline_broker_result_reader import (
    OfflineLocalOwnerResultReader,
)
from sora_commander_next.offline_broker_result_wire import (
    MAX_RESULT_FRAME, ResultWireRejected, decode_read_result,
)

ROOT = Path(__file__).resolve().parents[1]
APPROVE = ROOT / "tests" / "fixtures" / "phase9_dialog_approve.py"
DENY = ROOT / "tests" / "fixtures" / "phase9_dialog_deny.py"
STDOUT = b"A" * 4500
STDERR = b"B" * 700


@unittest.skipUnless(os.name == "nt", "Windows local Owner test only")
class OwnerResultPageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="sora-owner-page-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = OfflineApprovalLedger(self.root / "local-fence.sqlite3")
        self.addCleanup(self.db.close)
        self.request = Command(
            sys.executable,
            ("-I", "-S", "-c",
             "import sys;"
             "sys.stdout.buffer.write(b'A'*4500);sys.stdout.flush();"
             "sys.stderr.buffer.write(b'B'*700);sys.stderr.flush()"),
            str(self.root), timeout=5, output_limit=8192,
            request_id=secrets.token_hex(16))
        self.owner = DevOwnerAuthority(
            fixed_command=self.request, offline_ledger=self.db)
        self.addCleanup(self.owner.close)
        self.reader = OfflineLocalOwnerResultReader(self.owner)

    def frame(self, *, rid=None, sid=None, offset=0, op="read_result"):
        return json.dumps({
            "version": 2, "op": op,
            "request_id": self.request.request_id if rid is None else rid,
            "session_id": (self.owner._session or "a" * 48) if sid is None else sid,
            "offset": offset,
        }, separators=(",", ":")).encode("ascii")

    def approve_and_wait(self):
        self.owner.stage_local()
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT", APPROVE):
            self.assertEqual(self.owner.review_once_local(), "RUNNING")
        terminal = self.owner.wait_local(5)
        self.assertEqual(terminal["status"], "COMPLETED")
        self.assertEqual(terminal["exit_code"], 0)
        return terminal

    def test_actual_binary_stdout_stderr_paginate_after_reader_reconnection(self):
        self.approve_and_wait()
        first_frame = self.reader.read_local(self.frame())
        self.assertLessEqual(len(first_frame), MAX_RESULT_FRAME)
        first = decode_read_result(first_frame)
        self.assertEqual(first["status"], "COMPLETED")
        self.assertEqual(first["exit_code"], 0)
        self.assertEqual((first["offset"], first["next_offset"],
                          first["captured_bytes"]), (0, 4096, 5200))
        self.assertEqual(base64.b64decode(first["stdout_base64"]), STDOUT[:4096])
        self.assertEqual(base64.b64decode(first["stderr_base64"]), b"")
        # Reader/observer goes away, but the same Owner process & actual job
        # survive; create a fresh reader without re-executing.
        reconnected = OfflineLocalOwnerResultReader(self.owner)
        second = decode_read_result(reconnected.read_local(
            self.frame(offset=first["next_offset"])))
        self.assertEqual((second["offset"], second["next_offset"]), (4096, 5200))
        self.assertEqual(base64.b64decode(second["stdout_base64"]), STDOUT[4096:])
        self.assertEqual(base64.b64decode(second["stderr_base64"]), STDERR)
        third = decode_read_result(reconnected.read_local(self.frame(offset=5200)))
        self.assertEqual((third["offset"], third["next_offset"]), (5200, 5200))
        self.assertEqual(base64.b64decode(third["stdout_base64"]), b"")
        self.assertEqual(self.owner._jobs.list_sessions()["count"], 1)
        self.assertEqual(self.db.inspect_launch_attempt_data_only(
            self.request).status, "ALREADY_ATTEMPTED_TERMINAL")

    def test_owner_owned_status_is_metadata_only_no_output_read(self):
        self.approve_and_wait()
        with patch.object(self.owner._jobs, "read",
                          side_effect=AssertionError("metadata must not read output")) as read:
            state = self.owner.status_verified_local(
                self.request.request_id, self.owner._session)
            read.assert_not_called()
        self.assertEqual(set(state), {
            "request_id", "session_id", "status", "exit_code",
            "captured_bytes", "output_incomplete"})
        self.assertEqual(state["status"], "COMPLETED")
        self.assertEqual(state["exit_code"], 0)
        self.assertEqual(state["captured_bytes"], 5200)
        self.assertEqual(self.owner._jobs.list_sessions()["count"], 1)

    def test_status_cannot_query_other_session_or_unapproved_request(self):
        with self.assertRaises(LookupError):
            self.owner.status_verified_local(self.request.request_id, "f" * 48)
        self.approve_and_wait()
        for rid, sid in (
            ("f" * 32, self.owner._session),
            (self.request.request_id, "f" * 48),
            (self.request.request_id, True),
        ):
            with self.subTest(rid=rid, sid=sid), patch.object(
                    self.owner._jobs, "read",
                    side_effect=AssertionError("never read arbitrary session")) as read:
                with self.assertRaises(LookupError):
                    self.owner.status_verified_local(rid, sid)
                read.assert_not_called()
        with patch.object(self.owner._guard, "verify",
                          side_effect=PermissionError("simulated WTS lock")):
            with self.assertRaises(PermissionError):
                self.owner.status_verified_local(
                    self.request.request_id, self.owner._session)

    def test_without_approval_no_bytes_and_no_implicit_job(self):
        with patch.object(self.owner._jobs, "read",
                          side_effect=AssertionError("no raw read")) as read:
            with self.assertRaises(ResultWireRejected):
                self.reader.read_local(self.frame())
            self.owner.stage_local()
            with self.assertRaises(ResultWireRejected):
                self.reader.read_local(self.frame())
            read.assert_not_called()
        self.assertEqual(self.owner._jobs.list_sessions()["count"], 0)
        self.assertEqual(self.db.inspect_launch_attempt_data_only(
            self.request).status, "NOT_ATTEMPTED_DATA_ONLY")

    def test_denial_does_not_make_result_readable(self):
        self.owner.stage_local()
        with patch("sora_commander_next.owner_approval_broker.DIALOG_SCRIPT", DENY):
            self.assertEqual(self.owner.review_once_local(), "DENIED")
        with self.assertRaises(ResultWireRejected):
            self.reader.read_local(self.frame())
        self.assertEqual(self.owner._jobs.list_sessions()["count"], 0)

    def test_wrong_ids_privileged_operation_and_malformed_page_fail_closed(self):
        self.approve_and_wait()
        for data in (
            self.frame(rid="f" * 32),
            self.frame(sid="f" * 48),
            self.frame(offset=65537),
            self.frame(offset=True),
            self.frame(op="approve"),
            self.frame(op="start"),
            b'{"version":2,"op":"read_result","request_id":"a"}',
            self.frame()[:-1] + b',"retry":true}',
        ):
            with self.subTest(raw=data), self.assertRaises(ResultWireRejected):
                self.reader.read_local(data)
        self.assertEqual(self.owner._jobs.list_sessions()["count"], 1)

    def test_owner_lock_or_close_refuses_output(self):
        self.approve_and_wait()
        with patch.object(self.owner._guard, "verify",
                          side_effect=PermissionError("simulated lock")):
            with self.assertRaises(PermissionError):
                self.reader.read_local(self.frame())
        self.owner.close()
        with self.assertRaises(ResultWireRejected):
            self.reader.read_local(self.frame())

    def test_owner_close_while_formatting_discards_result(self):
        self.approve_and_wait()
        from sora_commander_next.offline_broker_result_reader import (
            _encode_checked_snapshot,
        )
        def close_during_format(*args):
            result = _encode_checked_snapshot(*args)
            self.owner.close()
            return result
        with patch("sora_commander_next.offline_broker_result_reader._encode_checked_snapshot",
                   side_effect=close_during_format):
            with self.assertRaises(PermissionError):
                self.reader.read_local(self.frame())

    def test_corrupt_manager_chunks_refused_and_no_new_job(self):
        self.approve_and_wait()
        real_read = self.owner._jobs.read
        def mutated(sid, offset):
            result = real_read(sid, offset)
            result["chunks"][0]["offset"] += 1
            return result
        with patch.object(self.owner._jobs, "read", side_effect=mutated):
            with self.assertRaises(ResultWireRejected):
                self.reader.read_local(self.frame())
        self.assertEqual(self.owner._jobs.list_sessions()["count"], 1)


if __name__ == "__main__":
    unittest.main()
