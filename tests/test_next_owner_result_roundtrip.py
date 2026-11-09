"""Test post-approval result transport with a synthetic broker outcome only.

Never clicks Owner UI or changes any live approval decision.
"""
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import build_server
from sora_commander_next.policy import Policy
from sora_commander_next.owner_review_dispatch import OwnerReviewDispatcher
from sora_commander_next.windows_owner import OwnerSessionGuard
from sora_commander_next.windows_owner_controller import WindowsOwnerController


class OwnerResultRoundtrip(TestCase):
    def test_synthetic_completed_job_can_be_fetched_using_review_session(self):
        with TemporaryDirectory() as directory:
            guard = OwnerSessionGuard()
            guard.verify()
            host = Host(policy=Policy(roots=(str(Path(directory).resolve()),)),
                        owner=guard.sid)
            try:
                dispatcher = OwnerReviewDispatcher(WindowsOwnerController(host))
                host.attach_owner_review_local(dispatcher)
                request = Command(sys.executable, ("--version",), directory)
                pending = host.approval.create(request, host._context())
                # This test injects an already-created job result. It does NOT
                # invoke a broker, click UI, or grant remote approval.
                job = host.jobs.start(request)
                self.assertEqual(job["status"], "RUNNING")
                with dispatcher._lock:
                    dispatcher._results[pending.pending_id] = {
                        "status": "RUNNING", "session_id": job["session_id"]}
                server = build_server(host)
                review = asyncio.run(server.call_tool("review_status", {
                    "pending_id": pending.pending_id}))[1]
                self.assertEqual(review["session_id"], job["session_id"])
                self.assertEqual(review["status"], "RUNNING")
                final = host.jobs.wait(job["session_id"])
                self.assertEqual(final["status"], "COMPLETED")
                self.assertEqual(final["exit_code"], 0)
                output = asyncio.run(server.call_tool("read_process_output", {
                    "session_id": review["session_id"]}))[1]
                self.assertEqual(output["status"], "COMPLETED")
                self.assertIn("Python", output["stdout"] + output["stderr"])
                self.assertEqual(output["exit_code"], 0)
                self.assertEqual(host.approval.matches(pending.pending_id,
                    request, host._context()), True)
            finally:
                host.jobs.close()
