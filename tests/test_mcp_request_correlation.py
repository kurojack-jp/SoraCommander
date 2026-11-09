"""Optional MCP request IDs and read-only recovery, no real Owner click or job.

The same 32hex ID is a correlation label, NEVER an approval token. This is
single-Host development behavior, not cross-process durable Broker admission.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import _validate_arguments

RID = "a" * 32


class MCPRequestCorrelationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="next-correlation-")
        self.addCleanup(directory.cleanup)
        self.cwd = str(Path(directory.name).resolve())
        self.command = {
            "executable": sys.executable, "argv": ["-c", "print('NEVER-RUN')"],
            "cwd": self.cwd, "request_id": RID,
        }

    def new_host(self):
        host = Host(policy=Policy(roots=(self.cwd,)))
        self.addCleanup(host.jobs.close)
        return host

    def test_wire_accepts_optional_id_without_changing_legacy_calls(self):
        for tool in ("run_command", "run_command_wait"):
            with self.subTest(tool=tool):
                _validate_arguments(tool, {
                    key: value for key, value in self.command.items()
                    if key != "request_id"})
                _validate_arguments(tool, dict(self.command))
        _validate_arguments("request_status", {"request_id": RID})

    def test_wire_rejects_invalid_or_tampered_ids_before_sdk_coercion(self):
        for invalid in (None, "", "AB" * 16, "g" * 32,
                        "a" * 31, True, 1234, "a" * 128):
            with self.subTest(value=invalid):
                for name in ("run_command", "run_command_wait"):
                    with self.assertRaises((ValueError, TypeError)):
                        _validate_arguments(name, {
                            **self.command, "request_id": invalid})
                with self.assertRaises(ValueError):
                    _validate_arguments("request_status", {
                        "request_id": invalid})
        with self.assertRaises(ValueError):
            _validate_arguments("request_status", {
                "request_id": RID, "approve": True})

    def test_review_retry_and_query_reuses_pending_without_job_launch(self):
        host = self.new_host()
        offer = Mock(return_value=True)
        host._owner_review_dispatcher = SimpleNamespace(
            offer_local=offer,
            review_status=lambda pending_id: {"status": "PENDING"},
        )
        with patch.object(host.jobs, "start") as start:
            first = host.run_command(background=True, **self.command)
            self.assertEqual(first["status"], "REVIEW")
            self.assertEqual(first["request_id"], RID)
            observed = host.request_status(RID)
            self.assertEqual(observed["pending_id"], first["pending_id"])
            self.assertEqual(observed["status"], "PENDING")
            second = host.run_command(background=True, **self.command)
            self.assertEqual(second["pending_id"], first["pending_id"])
            changed = {**self.command, "argv": ["-V"]}
            conflicting = host.run_command(background=True, **changed)
            self.assertEqual(conflicting["status"], "ERROR")
            self.assertEqual(conflicting["error"], "PermissionError")
            start.assert_not_called()
        self.assertEqual(len(host.approval.pending_local()), 1)
        self.assertEqual(offer.call_count, 2)

    def test_observe_completed_job_metadata_without_read_or_resubmit(self):
        host = self.new_host()
        expected = {
            "request_id": RID, "status": "COMPLETED",
            "session_id": "b" * 48, "exit_code": 0,
            "captured_bytes": 12, "output_incomplete": False,
        }
        with patch.object(host.jobs, "request_status_readonly",
                          return_value=expected) as lookup, \
             patch.object(host.jobs, "start") as start, \
             patch.object(host.jobs, "read") as read:
            self.assertEqual(host.request_status(RID), expected)
            lookup.assert_called_once_with(RID)
            start.assert_not_called()
            read.assert_not_called()

    def test_unknown_does_not_create_pending_or_start_job(self):
        host = self.new_host()
        with patch.object(host.jobs, "start") as start:
            self.assertEqual(host.request_status(RID),
                             {"request_id": RID, "status": "UNKNOWN"})
            self.assertEqual(host.request_status("bad"),
                             {"status": "ERROR", "error": "InvalidRequestId"})
            start.assert_not_called()
        self.assertEqual(host.approval.pending_local(), ())

    def test_job_status_query_failure_is_bounded(self):
        host = self.new_host()
        with patch.object(host.jobs, "request_status_readonly",
                          side_effect=OSError("private-path-hidden")):
            result = host.request_status(RID)
        self.assertEqual(result, {
            "request_id": RID, "status": "ERROR",
            "error": "RequestStatusUnavailable"})
        self.assertNotIn("private", str(result))


if __name__ == "__main__":
    unittest.main()
