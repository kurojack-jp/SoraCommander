"""Pure monitor presentation with synthetic snapshots; no OS or child jobs."""
from dataclasses import FrozenInstanceError, fields, replace
import unittest
from unittest.mock import patch

from sora_commander_next.job_status_snapshot import (
    JobStatus, JobStatusSnapshot, MAX_MONITOR_ROWS, monitor_view,
)


class MonitorViewTests(unittest.TestCase):
    def setUp(self):
        self.row = JobStatus("a" * 48, "RUNNING", None, 12, False)
        # Any accidental launch, capture, process probe or bootstrap is a failure.
        self.forbidden = [self.enterContext(patch(target, side_effect=AssertionError(
            "unexpected side effect"))) for target in (
                "subprocess.Popen", "subprocess.run", "threading.Thread.start",
                "sora_commander_next.job_status_snapshot.JobStatusSnapshotAdapter.snapshot",
                "sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job",
                "sora_commander_next.codex_jobs._process_identity",
                "sora_commander_next.codex_jobs.LocalCodexJobs.query",
            )]
        self.addCleanup(self.check_no_side_effects)

    def check_no_side_effects(self):
        for operation in self.forbidden:
            operation.assert_not_called()

    def view(self, **changes):
        return monitor_view(JobStatusSnapshot((replace(self.row, **changes),)))

    def test_process_completion_and_nonzero_exit_are_distinct(self):
        for code, expected in ((0, "COMPLETED"), (1, "FAILED"), (-9, "FAILED"),
                               (0xC0000005, "FAILED"), (None, "UNKNOWN")):
            with self.subTest(code=code):
                row = self.view(status="COMPLETED", exit_code=code).sessions[0]
                self.assertEqual(row.state, expected)
                self.assertEqual(row.exit_code, code)

    def test_explicit_failure_states_never_become_success_with_exit_zero(self):
        for status in ("FAILED", "CANCELLED", "TIMEOUT", "OUTPUT_LIMIT", "ERROR"):
            for code in (None, 0, 1):
                with self.subTest(status=status, code=code):
                    self.assertEqual(self.view(status=status, exit_code=code)
                                     .sessions[0].state, "FAILED")

    def test_unknown_and_foreign_statuses_are_not_promoted_or_echoed(self):
        for status in ("OUTCOME_UNKNOWN", "OUTPUT_INCOMPLETE", "UNKNOWN", "SUCCESS",
                       "STARTING", "SECRET_FUTURE_STATUS" * 10000, "", None, [], {}):
            with self.subTest(status_type=type(status).__name__):
                view = self.view(status=status, exit_code=0)
                self.assertEqual(view.sessions[0].state, "UNKNOWN")
                self.assertNotIn("SECRET", repr(view))

    def test_running_with_exit_code_is_unknown(self):
        self.assertEqual(self.view().sessions[0].state, "RUNNING")
        for code in (0, 1):
            self.assertEqual(self.view(exit_code=code).sessions[0].state, "UNKNOWN")

    def test_incomplete_output_never_reports_success(self):
        view = self.view(status="COMPLETED", exit_code=0, output_incomplete=True)
        self.assertEqual(view.sessions[0].state, "UNKNOWN")
        self.assertTrue(view.sessions[0].output_incomplete)
        self.assertEqual(self.view(status="COMPLETED", exit_code=1,
                                   output_incomplete=True).sessions[0].state, "FAILED")

    def test_malformed_exit_codes_are_sanitized_and_unknown(self):
        for code in (False, True, "SECRET", [], {}, 0.0, -2**31 - 1, 2**32):
            with self.subTest(code=code):
                row = self.view(status="COMPLETED", exit_code=code).sessions[0]
                self.assertEqual(row.state, "UNKNOWN")
                self.assertIsNone(row.exit_code)

    def test_invalid_metadata_is_rejected_without_echoing_values(self):
        for changes in ({"session_id": "SECRET"}, {"session_id": "A" * 48},
                        {"session_id": ["SECRET"]}, {"captured_bytes": True},
                        {"captured_bytes": -1}, {"captured_bytes": 2**63},
                        {"captured_bytes": "SECRET"}, {"output_incomplete": 0},
                        {"output_incomplete": "SECRET"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(
                    ValueError, "^invalid job metadata$"):
                self.view(**changes)

    def test_empty_snapshot_is_an_explicit_empty_view(self):
        view = monitor_view(JobStatusSnapshot(()))
        self.assertEqual((view.sessions, view.total_count, view.truncated), ((), 0, False))

    def test_limit_is_strictly_bounded(self):
        for limit in (True, False, None, 0, -1, 1.0, "32", MAX_MONITOR_ROWS + 1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                monitor_view(JobStatusSnapshot(()), limit=limit)

    def test_limit_preserves_order_and_reports_omitted_rows_without_reading_them(self):
        second = replace(self.row, session_id="b" * 48)
        # A deliberately invalid tail proves the omitted row is not inspected.
        snapshot = JobStatusSnapshot((self.row, second, object()))
        view = monitor_view(snapshot, limit=2)
        self.assertEqual(tuple(r.session_id for r in view.sessions), ("a" * 48, "b" * 48))
        self.assertEqual(view.total_count, 3)
        self.assertTrue(view.truncated)
        self.assertEqual(len(snapshot.sessions), 3)
        with self.assertRaises(ValueError):
            monitor_view(snapshot, limit=3)

    def test_default_and_maximum_row_caps(self):
        snapshot = JobStatusSnapshot(tuple(replace(self.row, session_id=f"{i:048x}")
                                          for i in range(MAX_MONITOR_ROWS + 1)))
        self.assertEqual(len(monitor_view(snapshot).sessions), 32)
        view = monitor_view(snapshot, limit=MAX_MONITOR_ROWS)
        self.assertEqual(len(view.sessions), MAX_MONITOR_ROWS)
        self.assertEqual(view.total_count, MAX_MONITOR_ROWS + 1)
        self.assertTrue(view.truncated)

    def test_frozen_allowlisted_view_is_detached_and_repeatable(self):
        snapshot = JobStatusSnapshot((self.row,))
        view = monitor_view(snapshot)
        self.assertEqual(view, monitor_view(snapshot))
        self.assertIs(snapshot.sessions[0], self.row)
        self.assertIsNot(view.sessions[0], self.row)
        self.assertEqual({f.name for f in fields(view.sessions[0])}, {
            "session_id", "state", "exit_code", "captured_bytes", "output_incomplete"})
        self.assertEqual(view.sessions[0].captured_bytes, 12)
        with self.assertRaises(FrozenInstanceError):
            view.sessions = ()
        with self.assertRaises(FrozenInstanceError):
            view.sessions[0].state = "COMPLETED"

    def test_requires_materialized_existing_snapshot(self):
        for invalid in (None, {}, (), JobStatusSnapshot([]),
                        JobStatusSnapshot(iter((self.row,)))):
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                monitor_view(invalid)


if __name__ == "__main__":
    unittest.main()
