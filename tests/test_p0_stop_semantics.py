"""Stop-result regression tests; no process, GUI, journal or native job created."""

from itertools import count
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.jobs import JobManager, _Job


class StopSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.manager = JobManager()
        self.host = Host()
        child = Mock(pid=123, returncode=None, stdin=None)
        child.poll.side_effect = lambda: child.returncode
        tree = Mock()
        tree.terminate.side_effect = lambda: setattr(child, "returncode", 1)
        request = Command(sys.executable, ("-I", "-S", "-q", "-i"),
                          str(ROOT), timeout=30, interactive=True)
        self.job = _Job("a" * 48, request, child, 200, tree=tree)
        self.job.thread = Mock()
        self.job.thread.is_alive.return_value = False
        # Exercise Host's separate DEV interactive session routing, without
        # installing approval hooks or admitting/starting any command.
        self.host._dev_interactive_jobs = self.manager
        self.manager._jobs[self.job.session_id] = self.job
        self.no_spawn = self.enterContext(patch(
            "sora_commander_next.jobs.subprocess.Popen",
            side_effect=AssertionError("regression tests must never spawn")))
        self.addCleanup(self.no_spawn.assert_not_called)

    def supervise(self):
        pieces = {
            self.job.child.stdout: iter((b"3\r\n", b"")),
            self.job.child.stderr: iter((b">>> ", b"")),
        }
        with patch("sora_commander_next.jobs._read_available",
                   side_effect=lambda pipe: next(pieces[pipe], b"")), patch(
                       "sora_commander_next.jobs.monotonic",
                       side_effect=count(100, .01)), patch(
                           "sora_commander_next.jobs.Event"):
            self.manager._supervise(self.job)
        return self.host.stop_process_wait(self.job.session_id, wait_seconds=0)

    def test_explicit_stop_before_deadline_is_cancelled_with_exit_one(self):
        self.assertEqual(self.host.stop_process(self.job.session_id)["status"],
                         "CANCELLING")
        result = self.supervise()
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["stdout"], "3\r\n")
        self.assertEqual(result["stderr"], ">>> ")
        self.assertFalse(result["output_incomplete"])
        self.job.tree.terminate.assert_called_once_with()
        self.job.tree.close.assert_called_once_with()

    def test_stop_after_timeout_preserves_terminal_result_and_output(self):
        self.job.deadline = 99
        result = self.supervise()
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertEqual(result["stop_request_status"], "TIMEOUT")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["stdout"], "3\r\n")
        self.assertFalse(self.job.cancel.is_set())
        self.assertEqual(self.host.stop_process_wait(
            self.job.session_id, wait_seconds=0), result)
        self.assertEqual(self.host.list_sessions()["sessions"][0]["status"],
                         "TIMEOUT")
        self.job.tree.terminate.assert_called_once_with()

    def test_cancel_observed_with_expired_deadline_keeps_cancel_priority(self):
        self.job.deadline = 99
        self.host.stop_process(self.job.session_id)
        self.assertEqual(self.supervise()["status"], "CANCELLED")

    def test_zero_wait_does_not_claim_stop_completed(self):
        result = self.host.stop_process_wait(self.job.session_id, wait_seconds=0)
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(result["stop_request_status"], "CANCELLING")
        self.assertIsNone(result["exit_code"])
        self.assertTrue(self.job.cancel.is_set())
        self.assertFalse(self.job.done.is_set())
        self.job.tree.terminate.assert_not_called()

    def test_tree_close_failure_preserves_unknown_despite_parent_exit(self):
        self.host.stop_process(self.job.session_id)
        self.job.tree.close.side_effect = OSError("synthetic close failure")
        result = self.supervise()
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(result["stop_request_status"], "OUTCOME_UNKNOWN")
        self.assertTrue(result["output_incomplete"])

    def test_tree_termination_failure_cannot_become_successful_stop(self):
        self.host.stop_process(self.job.session_id)
        self.job.tree.terminate.side_effect = OSError("synthetic terminate failure")
        result = self.supervise()
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertTrue(result["output_incomplete"])
        self.job.tree.close.assert_called_once_with()

    def test_parent_still_running_after_termination_stays_unknown(self):
        self.host.stop_process(self.job.session_id)
        self.job.tree.terminate.side_effect = None
        result = self.supervise()
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertIsNone(result["exit_code"])
        self.assertTrue(result["output_incomplete"])

    def test_parent_exit_still_terminates_owned_tree(self):
        self.job.child.returncode = 0
        # Job termination cannot change the already-exited parent's exit code.
        self.job.tree.terminate.side_effect = None
        result = self.supervise()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["exit_code"], 0)
        self.job.tree.terminate.assert_called_once_with()
        self.job.tree.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
