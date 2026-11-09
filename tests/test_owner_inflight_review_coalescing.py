"""Same-Host Owner review retries share one PENDING id and one GUI worker.

In-memory, non-authorizing tests. No Windows Owner click, protected Broker,
remote connector, real command execution or live grant is exercised.
"""
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event, Thread
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.approval import LocalApproval
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.owner_review_dispatch import OwnerReviewDispatcher
from sora_commander_next.policy import Policy
from sora_commander_next.windows_owner import OwnerSessionGuard
from sora_commander_next.windows_owner_controller import WindowsOwnerController


class PendingReviewCoalescenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="sora-owner-inflight-")
        self.addCleanup(temp.cleanup)
        self.cwd = str(Path(temp.name).resolve())
        self.request = Command(sys.executable, ("-c", "print(42)"),
                               self.cwd, request_id="inflight-request")
        self.context = ("owner", (self.cwd,), 1, "same-policy")

    def test_same_id_and_exact_context_reuses_one_pending_record(self):
        approval = LocalApproval()
        first = approval.create(self.request, self.context)
        second = approval.create(self.request, self.context)
        self.assertIs(first, second)
        self.assertEqual(len(approval.pending_local()), 1)
        self.assertEqual(first.pending_id, second.pending_id)

    def test_changed_command_or_policy_context_conflicts_without_second_record(self):
        approval = LocalApproval()
        prior = approval.create(self.request, self.context)
        mutations = (
            (replace(self.request, argv=("--version",)), self.context),
            (self.request, ("other-owner", *self.context[1:])),
            (self.request, ("owner", (self.cwd,), 2, "same-policy")),
            (self.request, ("owner", (self.cwd,), 1, "different-policy")),
        )
        for command, context in mutations:
            with self.subTest(command=command, context=context):
                with self.assertRaises(PermissionError):
                    approval.create(command, context)
        self.assertEqual(approval.pending_local(), (prior,))

    def test_concurrent_duplicate_create_one_pending_id(self):
        approval = LocalApproval()
        gate = Barrier(5)
        records = []
        def run():
            gate.wait()
            records.append(approval.create(self.request, self.context))
        threads = [Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(records), 4)
        self.assertEqual(len({p.pending_id for p in records}), 1)
        self.assertEqual(len(approval.pending_local()), 1)

    def test_recent_consumed_or_rejected_request_id_cannot_reopen_review(self):
        clock = [100.0]
        approval = LocalApproval(clock=lambda: clock[0])
        original = approval.create(self.request, self.context)
        self.assertIs(approval.consume_local(
            original.pending_id, self.context), original)
        self.assertEqual(approval.pending_local(), ())
        self.assertIs(approval.create(self.request, self.context), original)
        with self.assertRaises(PermissionError):
            approval.create(replace(self.request, argv=("-V",)), self.context)
        clock[0] += 120
        self.assertIs(approval.create(self.request, self.context), original)

    def test_expired_review_is_not_restarted_inside_recent_window(self):
        clock = [100.0]
        approval = LocalApproval(clock=lambda: clock[0])
        original = approval.create(self.request, self.context)
        clock[0] += 61
        self.assertIs(approval.create(self.request, self.context), original)
        self.assertEqual(approval.pending_local(), ())
        # Bound of this local-only UX cache; durable Broker owns long-term replay.
        clock[0] += 600
        new = approval.create(self.request, self.context)
        self.assertNotEqual(new.pending_id, original.pending_id)

    def test_completed_owner_denial_reuses_original_observation(self):
        guard = OwnerSessionGuard()
        guard.verify()
        host = Host(policy=Policy(roots=(self.cwd,)), owner=guard.sid)
        self.addCleanup(host.jobs.close)
        dispatcher = OwnerReviewDispatcher(WindowsOwnerController(host))
        host.attach_owner_review_local(dispatcher)
        payload = {
            "executable": self.request.executable,
            "argv": list(self.request.argv),
            "cwd": self.cwd,
            "request_id": self.request.request_id,
        }

        def simulated_deny(pending_id):
            host.approval.reject_local(pending_id)
            return {"status": "DENIED"}

        with patch.object(dispatcher._broker, "review_once",
                          side_effect=simulated_deny) as broker_review:
            first = host.run_command(background=True, **payload)
            self.assertEqual(first["status"], "REVIEW")
            dispatcher._active.join(timeout=3)
            self.assertFalse(dispatcher._active.is_alive())
            self.assertEqual(host.review_status(first["pending_id"])["status"],
                             "DENIED")
            second = host.run_command(background=True, **payload)
            self.assertEqual(second["status"], "REVIEW", second)
            self.assertEqual(second["pending_id"], first["pending_id"])
            self.assertEqual(host.await_command_result(
                first["pending_id"], wait_seconds=0)["status"], "DENIED")
            mutated = {**payload, "argv": ["--version"]}
            self.assertEqual(host.run_command(
                background=True, **mutated)["error"], "PermissionError")
            broker_review.assert_called_once_with(first["pending_id"])
            self.assertEqual(len(host.approval.pending_local()), 0)

    def test_completed_synthetic_owner_acceptance_reuses_status_not_dialog(self):
        guard = OwnerSessionGuard()
        guard.verify()
        host = Host(policy=Policy(roots=(self.cwd,)), owner=guard.sid)
        self.addCleanup(host.jobs.close)
        dispatcher = OwnerReviewDispatcher(WindowsOwnerController(host))
        host.attach_owner_review_local(dispatcher)
        payload = {"executable": self.request.executable,
                   "argv": list(self.request.argv), "cwd": self.cwd,
                   "request_id": self.request.request_id}
        # Synthetic receipt only. This deliberately does NOT launch a job.
        fake_session = "a" * 48

        def simulated_approval(pending_id):
            host.approval.consume_local(pending_id, host._context())
            return {"status": "RUNNING", "session_id": fake_session}

        with patch.object(dispatcher._broker, "review_once",
                          side_effect=simulated_approval) as review:
            first = host.run_command(background=True, **payload)
            dispatcher._active.join(timeout=3)
            self.assertEqual(host.review_status(first["pending_id"]),
                             {"status": "RUNNING", "session_id": fake_session})
            second = host.run_command(background=True, **payload)
            self.assertEqual(second["status"], "REVIEW")
            self.assertEqual(second["pending_id"], first["pending_id"])
            review.assert_called_once_with(first["pending_id"])
            self.assertFalse(host.approval.pending_local())

    def test_evicted_completed_review_fails_closed_without_new_dialog(self):
        guard = OwnerSessionGuard()
        guard.verify()
        host = Host(policy=Policy(roots=(self.cwd,)), owner=guard.sid)
        self.addCleanup(host.jobs.close)
        dispatcher = OwnerReviewDispatcher(WindowsOwnerController(host))
        host.attach_owner_review_local(dispatcher)
        payload = {"executable": self.request.executable,
                   "argv": list(self.request.argv), "cwd": self.cwd,
                   "request_id": self.request.request_id}

        def simulated_deny(pending_id):
            host.approval.reject_local(pending_id)
            return {"status": "DENIED"}

        with patch.object(dispatcher._broker, "review_once",
                          side_effect=simulated_deny) as review:
            original = host.run_command(background=True, **payload)
            dispatcher._active.join(timeout=3)
            self.assertEqual(original["status"], "REVIEW")
            self.assertFalse(host.approval.pending_local())
            with dispatcher._lock:
                dispatcher._results.pop(original["pending_id"])
            replay = host.run_command(background=True, **payload)
            self.assertEqual(replay["status"], "ERROR")
            self.assertEqual(replay["error"], "PermissionError")
            review.assert_called_once()
            self.assertFalse(host.approval.pending_local())

    def test_same_host_retries_do_not_start_second_owner_dialog_thread(self):
        guard = OwnerSessionGuard()
        guard.verify()
        host = Host(policy=Policy(roots=(self.cwd,)), owner=guard.sid)
        self.addCleanup(host.jobs.close)
        dispatcher = OwnerReviewDispatcher(WindowsOwnerController(host))
        host.attach_owner_review_local(dispatcher)
        entered, finish = Event(), Event()

        def synthetic_review(pending_id):
            entered.set()
            if not finish.wait(timeout=4):
                return {"status": "ERROR"}
            return {"status": "DENIED"}

        request_payload = {
            "executable": self.request.executable,
            "argv": list(self.request.argv),
            "cwd": self.request.cwd,
            "request_id": self.request.request_id
        }
        with patch.object(dispatcher._broker, "review_once",
                          side_effect=synthetic_review) as review:
            try:
                first = host.run_command(background=True, **request_payload)
                self.assertEqual(first["status"], "REVIEW", first)
                self.assertTrue(entered.wait(timeout=2))
                second = host.run_command(background=True, **request_payload)
                self.assertEqual(second["status"], "REVIEW", second)
                self.assertEqual(second["pending_id"], first["pending_id"])
                self.assertEqual(dispatcher.review_status(
                    first["pending_id"])["status"], "PENDING")
                review.assert_called_once_with(first["pending_id"])
            finally:
                finish.set()
                if dispatcher._active is not None:
                    dispatcher._active.join(timeout=5)
            review.assert_called_once()
            self.assertEqual(dispatcher.review_status(
                first["pending_id"])["status"], "DENIED")


if __name__ == "__main__":
    unittest.main()
