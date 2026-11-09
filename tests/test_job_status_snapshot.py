"""Offline snapshot contracts using real manager/job objects, no child launch."""
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, fields
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sora_commander_next.job_status_snapshot import JobStatusSnapshotAdapter
from sora_commander_next.jobs import JobManager, _Job


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.manager = JobManager()
        self.adapter = JobStatusSnapshotAdapter(self.manager)
        stack = self.enterContext(ExitStack())
        self.forbidden = []
        for target in ("subprocess.Popen", "subprocess.run",
                       "sora_commander_next.jobs.Thread.start"):
            self.forbidden.append(stack.enter_context(patch(
                target, side_effect=AssertionError("unexpected process/thread launch"))))
        for name in ("_prune", "start", "read", "list_sessions", "stop", "wait",
                     "close", "_snapshot_journal", "previous_sessions"):
            self.forbidden.append(stack.enter_context(patch.object(
                self.manager, name, side_effect=AssertionError(name))))
        self.manager.clock = Mock(side_effect=AssertionError("clock read"))
        self.forbidden.append(self.manager.clock)
        self.addCleanup(self.assert_no_lifecycle_calls)

    def assert_no_lifecycle_calls(self):
        for operation in self.forbidden:
            operation.assert_not_called()

    def job(self, number=1, status="RUNNING", exit_code=None):
        child = Mock()
        child.poll.side_effect = AssertionError("child poll")
        child.pid = 4321
        request = SimpleNamespace(argv=("SECRET_ARG",), cwd="SECRET_CWD",
                                  executable="SECRET_EXE", request_id="SECRET_REQUEST")
        job = _Job(f"{number:048x}", request, child, 0,
                   status=status, exit_code=exit_code)
        job.chunks = [(0, "stdout", b"SECRET_OUTPUT")]
        self.manager._jobs[job.session_id] = job
        self.addCleanup(child.assert_not_called)
        self.addCleanup(child.poll.assert_not_called)
        return job

    def test_empty_and_closed_manager_are_readable_without_teardown(self):
        for closed in (False, True):
            self.manager._closed = closed
            snapshot = self.adapter.snapshot()
            self.assertEqual(snapshot.sessions, ())
            self.assertEqual(snapshot.count, 0)
            self.assertEqual(self.manager._closed, closed)

    def test_exact_metadata_allowlist(self):
        job = self.job()
        job.captured = 13
        job.output_incomplete = True
        snapshot = self.adapter.snapshot()
        self.assertEqual(snapshot.count, 1)
        row = snapshot.sessions[0]
        self.assertEqual({f.name for f in fields(row)}, {
            "session_id", "status", "exit_code", "captured_bytes", "output_incomplete"})
        self.assertEqual((row.session_id, row.status, row.exit_code,
                          row.captured_bytes, row.output_incomplete),
                         (job.session_id, "RUNNING", None, 13, True))
        self.assertNotIn("SECRET", repr(snapshot))
        self.assertNotIn("4321", repr(snapshot))

    def test_recorded_states_are_preserved_without_success_mapping(self):
        states = ("RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
                  "OUTPUT_LIMIT", "OUTPUT_INCOMPLETE", "OUTCOME_UNKNOWN", "ERROR",
                  "FUTURE_STATE")
        for number, state in enumerate(states, 1):
            self.job(number, state, 0 if state == "COMPLETED" else None)
        self.assertEqual(tuple(row.status for row in self.adapter.snapshot().sessions), states)

    def test_finished_expired_jobs_remain_retained(self):
        job = self.job(status="COMPLETED", exit_code=0)
        job.done.set()
        job.finished_at = -10000
        self.assertEqual(self.adapter.snapshot().sessions[0].status, "COMPLETED")
        self.assertIs(self.manager._jobs[job.session_id], job)
        self.assertTrue(job.done.is_set())
        self.assertFalse(job.cancel.is_set())

    def test_snapshots_are_immutable_and_detached(self):
        job = self.job()
        first = self.adapter.snapshot()
        with self.assertRaises(FrozenInstanceError):
            first.sessions = ()
        with self.assertRaises(FrozenInstanceError):
            first.sessions[0].status = "COMPLETED"
        job.status, job.exit_code, job.captured = "COMPLETED", 0, 99
        second = self.adapter.snapshot()
        self.assertEqual((first.sessions[0].status, first.sessions[0].captured_bytes),
                         ("RUNNING", 0))
        self.assertEqual((second.sessions[0].status, second.sessions[0].captured_bytes),
                         ("COMPLETED", 99))
        self.manager._jobs.clear()
        self.assertEqual(first.count, 1)
        self.assertEqual(self.adapter.snapshot().count, 0)

    def test_capture_does_not_consume_output_or_set_events(self):
        job = self.job()
        chunks = list(job.chunks)
        for _ in range(3):
            self.assertEqual(self.adapter.snapshot().count, 1)
        self.assertEqual(job.chunks, chunks)
        self.assertEqual(job.captured, 0)
        self.assertFalse(job.cancel.is_set())
        self.assertFalse(job.done.is_set())

    def test_capture_acquires_manager_then_job_lock(self):
        events = []

        class RecordingLock:
            def __init__(self, name):
                self.lock, self.name = RLock(), name

            def __enter__(self):
                self.lock.acquire()
                events.append("enter " + self.name)

            def __exit__(self, *exc):
                events.append("exit " + self.name)
                self.lock.release()

        job = self.job()
        self.manager._lock = RecordingLock("manager")
        job.lock = RecordingLock("job")
        self.adapter.snapshot()
        self.assertEqual(events, ["enter manager", "enter job", "exit job", "exit manager"])

    def test_capture_failure_propagates_instead_of_reporting_empty_success(self):
        job = self.job()
        job.lock = Mock()
        job.lock.__enter__ = Mock(side_effect=RuntimeError("unavailable"))
        job.lock.__exit__ = Mock()
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.adapter.snapshot()

    def test_full_capacity_is_copied_without_eviction(self):
        self.manager.capacity = 256
        for number in range(256):
            self.job(number)
        snapshot = self.adapter.snapshot()
        self.assertEqual(snapshot.count, 256)
        self.assertEqual(len({row.session_id for row in snapshot.sessions}), 256)
        self.assertEqual(len(self.manager._jobs), 256)

    def test_requires_existing_manager(self):
        for invalid in (None, {}, object()):
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                JobStatusSnapshotAdapter(invalid)


if __name__ == "__main__":
    unittest.main()
