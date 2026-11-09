"""P0: an Owner review that cannot be offered must say so, not a bare PermissionError.

A locked/disconnected Owner desktop or an already-active review dialog is a
NOT_STARTED outcome. Callers must be able to tell it apart from a sandbox or
Git denial without any exception text, and must never be told to retry.
No GUI, no process launch, no file fixture: the dispatcher is a local stub.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy

GIT = r"C:\Program Files\Git\cmd\git.exe"
ARGV = ["status", "--short", "--branch"]
SECRET = "Owner desktop locked, disconnected or unknown"


class _Dispatcher:
    def __init__(self, outcome):
        self.outcome, self.offered = outcome, []

    def offer_local(self, pending_id):
        self.offered.append(pending_id)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class OwnerReviewUnavailableTests(unittest.TestCase):
    def _host(self, outcome):
        host = Host(policy=Policy(roots=(str(ROOT),)), owner="owner")
        host._owner_review_dispatcher = _Dispatcher(outcome)
        self.addCleanup(host.jobs.close)
        return host

    def _assert_not_started(self, host, result, reason):
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error"], "OwnerReviewUnavailable")
        self.assertEqual(result["reason"], reason)
        self.assertEqual(result["launch_outcome"], "NOT_STARTED")
        self.assertIs(result["auto_retry"], False)
        self.assertNotIn("pending_id", result)
        self.assertNotIn("session_id", result)
        self.assertNotIn(SECRET, repr(result))
        self.assertEqual(host.approval.pending_local(), ())
        self.assertEqual(host.jobs._jobs, {})
        self.assertEqual(len(host._owner_review_dispatcher.offered), 1)

    def test_locked_owner_session_is_distinct_not_started(self):
        host = self._host(PermissionError(SECRET))
        result = host.run_command(executable=GIT, argv=ARGV, cwd=str(ROOT))
        self._assert_not_started(host, result, "OWNER_SESSION_UNAVAILABLE")

    def test_busy_review_dialog_is_distinct_not_started(self):
        host = self._host(False)
        result = host.run_command(executable=GIT, argv=ARGV, cwd=str(ROOT))
        self._assert_not_started(host, result, "REVIEW_BUSY")

    def test_unexpected_offer_failure_is_not_labeled_busy_or_locked(self):
        host = self._host(RuntimeError(SECRET))
        result = host.run_command(executable=GIT, argv=ARGV, cwd=str(ROOT))
        self._assert_not_started(host, result, "OWNER_REVIEW_FAILED")

    def test_wait_api_returns_same_outcome_without_waiting(self):
        host = self._host(PermissionError(SECRET))
        result = host.run_command_wait(executable=GIT, argv=ARGV, cwd=str(ROOT),
                                       wait_seconds=0)
        self._assert_not_started(host, result, "OWNER_SESSION_UNAVAILABLE")

    def test_consumed_review_replay_never_claims_not_started(self):
        # The original review may have been approved and launched; a later
        # replay of the same request must not report "nothing started".
        for outcome in (PermissionError(SECRET), False, RuntimeError(SECRET)):
            with self.subTest(outcome=type(outcome).__name__):
                host = self._host(True)
                payload = {"executable": GIT, "argv": ARGV, "cwd": str(ROOT),
                           "request_id": "b" * 32}
                first = host.run_command(**payload)
                self.assertEqual(first["status"], "REVIEW")
                host.approval.consume_local(first["pending_id"], host._context())
                host._owner_review_dispatcher.outcome = outcome
                replay = host.run_command(**payload)
                self.assertEqual(replay, {"request_id": "b" * 32, "status": "ERROR",
                                          "error": "PermissionError"})

    def test_request_id_conflict_is_not_relabeled_as_owner_unavailable(self):
        host = self._host(True)
        request_id = "a" * 32
        first = host.run_command(executable=GIT, argv=ARGV, cwd=str(ROOT),
                                 request_id=request_id)
        self.assertEqual(first["status"], "REVIEW")
        changed = host.run_command(executable=GIT, argv=["status"], cwd=str(ROOT),
                                   request_id=request_id)
        self.assertEqual(changed, {"request_id": request_id, "status": "ERROR",
                                   "error": "PermissionError"})


if __name__ == "__main__":
    unittest.main()
