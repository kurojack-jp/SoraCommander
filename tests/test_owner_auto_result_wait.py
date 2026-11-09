"""Approval-to-output in one MCP call: fake local review, REAL bounded Windows Jobs.

All fake review returns are test-only, never Owner authentication or grants.
No local GUI is shown, no protected production storage/tunnel is touched.
"""
import asyncio
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.jobs import JobManager
from sora_commander_next.mcp_adapter import _validate_arguments, build_server
from sora_commander_next.owner_review_dispatch import OwnerReviewDispatcher
from sora_commander_next.policy import Pattern, Policy
from sora_commander_next.windows_owner import OwnerSessionGuard
from sora_commander_next.windows_owner_controller import WindowsOwnerController


class OwnerResultWaitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="sora-auto-owner-wait-")
        self.addCleanup(tmp.cleanup)
        self.root = str(Path(tmp.name).resolve())
        self.guard = OwnerSessionGuard()
        self.guard.verify()
        self.jobs = JobManager(max_concurrent=2, capacity=8)
        self.addCleanup(self.jobs.close)
        self.host = Host(policy=Policy(roots=(self.root,)),
                         owner=self.guard.sid, jobs=self.jobs)
        self.dispatcher = OwnerReviewDispatcher(WindowsOwnerController(self.host))
        self.host.attach_owner_review_local(self.dispatcher)

    def payload(self, code="print('owner-auto-wait')", **other):
        return {
            "executable": sys.executable,
            "argv": ["-I", "-S", "-c", code],
            "cwd": self.root,
            "timeout": 5.0,
            **other,
        }

    def synthetic_approved(self, delay=0.0):
        """Mock ONLY the human click broker; actual job is real and bounded."""
        def approve(pending_id):
            if delay:
                time.sleep(delay)
            pending = next(record for record in self.host.approval.pending_local()
                           if record.pending_id == pending_id)
            return {**self.jobs.start(pending.request), "decision": "REVIEW"}
        return patch.object(self.dispatcher._broker, "review_once",
                            side_effect=approve)

    def test_approval_to_command_output_in_one_host_call(self):
        with self.synthetic_approved() as approval:
            result = self.host.run_command_wait(
                **self.payload(), wait_seconds=5)
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("owner-auto-wait", result["stdout"])
        self.assertEqual(result["decision"], "REVIEW")
        self.assertEqual(len(result["pending_id"]), 32)
        approval.assert_called_once()
        self.assertEqual(self.host.await_command_result(
            result["pending_id"], wait_seconds=0)["session_id"],
            result["session_id"])

    def test_pending_review_resume_never_starts_twice(self):
        with self.synthetic_approved(delay=.22) as approval:
            first = self.host.run_command_wait(
                **self.payload(), wait_seconds=.01)
            self.assertEqual(first["status"], "PENDING", first)
            second = self.host.await_command_result(
                first["pending_id"], wait_seconds=3)
        self.assertEqual(second["status"], "COMPLETED", second)
        self.assertIn("owner-auto-wait", second["stdout"])
        approval.assert_called_once()
        self.assertEqual(self.host.list_sessions()["count"], 1)

    def test_completed_review_long_job_remains_resumable(self):
        # Job start time varies with machine load, so the short wait may end
        # before or after the review result is published. Both are resumable.
        code = "import time; time.sleep(2); print('job-eventually-finished')"
        with self.synthetic_approved() as approval:
            first = self.host.run_command_wait(
                **self.payload(code), wait_seconds=.08)
            self.assertIn(first["status"], ("PENDING", "RUNNING"), first)
            running = first
            deadline = time.monotonic() + 30
            while running["status"] == "PENDING" and time.monotonic() < deadline:
                running = self.host.await_command_result(
                    first["pending_id"], wait_seconds=.05)
            self.assertEqual(running["status"], "RUNNING", running)
            self.assertTrue(running.get("session_id"))
            resumed = self.host.await_command_result(
                first["pending_id"], wait_seconds=30)
        self.assertEqual(resumed["status"], "COMPLETED", resumed)
        self.assertIn("job-eventually-finished", resumed["stdout"])
        self.assertEqual(resumed["session_id"], running["session_id"])
        self.assertEqual(self.host.list_sessions()["count"], 1)
        approval.assert_called_once()

    def test_rejection_is_terminal_and_does_not_start_job(self):
        with (patch.object(self.dispatcher._broker, "review_once",
                           return_value={"status": "DENIED"}) as rejected,
              patch.object(self.jobs, "start") as launched):
            result = self.host.run_command_wait(
                **self.payload(), wait_seconds=2)
        self.assertEqual(result["status"], "DENIED")
        self.assertEqual(self.host.await_command_result(
            result["pending_id"], wait_seconds=0)["status"], "DENIED")
        rejected.assert_called_once()
        launched.assert_not_called()

    def test_broker_failure_is_terminal_and_does_not_start_job(self):
        with (patch.object(self.dispatcher._broker, "review_once",
                           side_effect=RuntimeError("test failure")),
              patch.object(self.jobs, "start") as launched):
            result = self.host.run_command_wait(
                **self.payload(), wait_seconds=2)
        # A caller cannot prove whether an unexpected broker failure happened
        # before or after dispatch. Preserve uncertainty even in this fixture.
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(result["launch_outcome"], "UNKNOWN")
        launched.assert_not_called()

    def test_denied_scope_does_not_open_owner_review(self):
        with (tempfile.TemporaryDirectory() as forbidden,
              patch.object(self.dispatcher._broker, "review_once") as review):
            result = self.host.run_command_wait(
                **self.payload(cwd=forbidden), wait_seconds=2)
        self.assertEqual(result["status"], "DENY")
        review.assert_not_called()
        self.assertEqual(self.host.list_sessions()["count"], 0)

    def test_wait_zero_returns_pending_and_resumable(self):
        with self.synthetic_approved(delay=.20) as approval:
            result = self.host.run_command_wait(**self.payload(), wait_seconds=0)
            self.assertEqual(result["status"], "PENDING")
            completed = self.host.await_command_result(
                result["pending_id"], wait_seconds=3)
        self.assertEqual(completed["status"], "COMPLETED")
        approval.assert_called_once()

    def test_no_owner_dispatcher_does_not_silently_approve(self):
        host = Host(policy=Policy(roots=(self.root,)),
                    owner=self.guard.sid, jobs=self.jobs)
        with patch.object(self.jobs, "start") as spawn:
            result = host.run_command_wait(
                **self.payload(), wait_seconds=.01)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(host.await_command_result(
            result["pending_id"], wait_seconds=.01)["status"], "DENY")
        spawn.assert_not_called()

    def test_fixed_auto_keeps_policy_and_does_not_review(self):
        self.host.policy = Policy(
            roots=(self.root,),
            auto=(Pattern(sys.executable, ("--version",)),))
        with patch.object(self.dispatcher._broker, "review_once") as review:
            result = self.host.run_command_wait(
                executable=sys.executable, argv=["--version"], cwd=self.root,
                timeout=5, wait_seconds=3)
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(result["decision"], "AUTO")
        self.assertEqual(result["exit_code"], 0)
        review.assert_not_called()

    def test_invalid_wait_never_dispatches(self):
        for value in (True, None, -1, 91, float("nan"), "5"):
            with self.subTest(value=value), patch.object(
                    self.host, "run_command") as started:
                result = self.host.run_command_wait(
                    **self.payload(), wait_seconds=value)
                self.assertEqual(result["error"], "InvalidWaitSeconds")
                started.assert_not_called()
        self.assertEqual(self.host.await_command_result(
            "bad", wait_seconds=1)["error"], "InvalidPendingId")

    def test_job_wait_for_is_read_only_and_bounded(self):
        request = Command(sys.executable, (
            "-I", "-S", "-c", "import time;time.sleep(.25);print('done')"),
            self.root, timeout=2)
        session = self.jobs.start(request)["session_id"]
        with patch.object(self.jobs, "start") as no_more:
            partial = self.jobs.wait_for(session, 0)
            self.assertEqual(partial["session_id"], session)
            done = self.jobs.wait_for(session, 2)
            self.assertEqual(done["status"], "COMPLETED")
            self.assertIn("done", done["stdout"])
            no_more.assert_not_called()
        for invalid in (True, None, -1, 91, float("inf")):
            with self.assertRaises(ValueError):
                self.jobs.wait_for(session, invalid)

    def test_mcp_schema_rejects_approval_and_invalid_wait(self):
        valid = self.payload()
        _validate_arguments("run_command_wait", valid)
        _validate_arguments("await_command_result",
                            {"pending_id": "a" * 32})
        for extra in (
            {"approve": True}, {"background": True},
            {"wait_seconds": -1}, {"wait_seconds": True},
            {"wait_seconds": float("inf")}, {"wait_seconds": "50"},
            {"wait_seconds": 91},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                _validate_arguments("run_command_wait", valid | extra)
        for invalid in (
            {"pending_id": "a" * 32, "decision": "APPROVE"},
            {"pending_id": "a" * 32, "wait_seconds": True},
            {"pending_id": 3},
        ):
            with self.assertRaises(ValueError):
                _validate_arguments("await_command_result", invalid)

    def test_mcp_actual_async_adapter_tool_roundtrip(self):
        server = build_server(self.host)
        tools = {tool.name for tool in server._tool_manager.list_tools()}
        self.assertIn("run_command_wait", tools)
        self.assertIn("await_command_result", tools)
        self.assertNotIn("approve", tools)
        with self.synthetic_approved() as approved:
            response = asyncio.run(server.call_tool(
                "run_command_wait",
                {**self.payload(), "wait_seconds": 5}))[1]
        self.assertEqual(response["status"], "COMPLETED", response)
        self.assertEqual(response["decision"], "REVIEW")
        self.assertIn("owner-auto-wait", response["stdout"])
        followup = asyncio.run(server.call_tool(
            "await_command_result",
            {"pending_id": response["pending_id"],
             "wait_seconds": 0}))[1]
        self.assertEqual(followup["session_id"], response["session_id"])
        approved.assert_called_once()


if __name__ == "__main__":
    unittest.main()
