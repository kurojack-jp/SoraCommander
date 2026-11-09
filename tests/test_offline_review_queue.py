"""Inert single-instance FIFO review scheduling tests; NO executable authority.

These tests never open an Owner GUI, talk to MCP, instantiate a Broker,
inspect protected Windows directories, execute jobs or issue real grants.
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.offline_review_queue import OfflineReviewQueue


def digest(label):
    return sha256(label.encode("ascii")).hexdigest()


class OfflineReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.queue = OfflineReviewQueue(pending_capacity=4,
                                        history_capacity=8)

    def test_fifo_one_active_and_distinct_requests(self):
        first = self.queue.submit_data_only("one", digest("one"))
        second = self.queue.submit_data_only("two", digest("two"))
        self.assertEqual(first.status, "QUEUED_DATA_ONLY")
        self.assertEqual(second.status, "QUEUED_DATA_ONLY")
        active = self.queue.take_next_data_only()
        self.assertEqual(active.request_id, "one")
        self.assertEqual(active.status, "IN_REVIEW_DATA_ONLY")
        self.assertIsNone(self.queue.take_next_data_only())
        finished = self.queue.complete_data_only(
            active.queue_id, "DENIED_DATA_ONLY")
        self.assertEqual(finished.status, "DENIED_DATA_ONLY")
        next_item = self.queue.take_next_data_only()
        self.assertEqual(next_item.request_id, "two")
        self.assertEqual(self.queue.complete_data_only(
            next_item.queue_id, "REVIEWED_DATA_ONLY").status,
            "REVIEWED_DATA_ONLY")
        self.assertIsNone(self.queue.take_next_data_only())

    def test_duplicate_inflight_and_completed_never_enqueue_twice(self):
        first = self.queue.submit_data_only("same", digest("scope"))
        again = self.queue.submit_data_only("same", digest("scope"))
        self.assertEqual(again, first)
        active = self.queue.take_next_data_only()
        self.assertEqual(self.queue.submit_data_only(
            "same", digest("scope")), active)
        self.queue.complete_data_only(active.queue_id, "REVIEWED_DATA_ONLY")
        recorded = self.queue.submit_data_only("same", digest("scope"))
        self.assertEqual(recorded.status, "REVIEWED_DATA_ONLY")
        self.assertEqual(recorded.queue_id, first.queue_id)
        self.assertIsNone(self.queue.take_next_data_only())

    def test_conflicting_id_or_malformed_fingerprint_is_denied(self):
        self.queue.submit_data_only("same", digest("scope"))
        self.assertEqual(self.queue.submit_data_only(
            "same", digest("modified")).status, "DENY_CONFLICT")
        self.assertEqual(self.queue.observe_data_only(
            "same", digest("other")).status, "DENY_CONFLICT")
        for bad_id, bad_digest in (
            ("", digest("a")), (None, digest("a")),
            ("bad\0id", digest("a")), ("x" * 129, digest("a")),
            ("okay", "x" * 64), ("okay", None)):
            with self.subTest(bad_id=bad_id):
                self.assertEqual(self.queue.submit_data_only(
                    bad_id, bad_digest).status, "DENY_INVALID")

    def test_capacity_fails_closed_and_never_evicts_history(self):
        q = OfflineReviewQueue(pending_capacity=2, history_capacity=2)
        a = q.submit_data_only("a", digest("a"))
        b = q.submit_data_only("b", digest("b"))
        self.assertEqual(q.submit_data_only("c", digest("c")).status, "BUSY")
        first = q.take_next_data_only()
        q.complete_data_only(first.queue_id, "DENIED_DATA_ONLY")
        second = q.take_next_data_only()
        q.complete_data_only(second.queue_id, "DENIED_DATA_ONLY")
        self.assertEqual(q.submit_data_only("c", digest("c")).status, "BUSY")
        self.assertEqual(q.submit_data_only("a", digest("a")).queue_id,
                         a.queue_id)
        self.assertEqual(q.submit_data_only("b", digest("b")).queue_id,
                         b.queue_id)

    def test_completion_requires_exact_active_queue_ticket(self):
        a = self.queue.submit_data_only("a", digest("a"))
        self.assertEqual(self.queue.complete_data_only(
            a.queue_id, "REVIEWED_DATA_ONLY").status, "DENY_NO_ACTIVE")
        active = self.queue.take_next_data_only()
        self.assertEqual(self.queue.complete_data_only(
            "other", "REVIEWED_DATA_ONLY").status, "DENY_WRONG_TICKET")
        self.assertEqual(self.queue.complete_data_only(
            active.queue_id, "APPROVED").status, "DENY_INVALID")
        self.assertEqual(self.queue.observe_data_only(
            "a", digest("a")).status, "IN_REVIEW_DATA_ONLY")
        self.assertEqual(self.queue.complete_data_only(
            active.queue_id, "UNKNOWN_DATA_ONLY").status,
            "UNKNOWN_DATA_ONLY")
        self.assertEqual(self.queue.complete_data_only(
            active.queue_id, "DENIED_DATA_ONLY").status, "DENY_NO_ACTIVE")

    def test_simultaneous_threads_submit_same_request_one_queue_entry(self):
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(
                lambda _: self.queue.submit_data_only("same", digest("a")),
                range(30)))
        self.assertEqual(len({r.queue_id for r in results}), 1)
        self.assertEqual({r.status for r in results}, {"QUEUED_DATA_ONLY"})
        self.assertEqual(self.queue.take_next_data_only().request_id, "same")
        self.assertIsNone(self.queue.take_next_data_only())

    def test_close_marks_unfinished_unknown_never_replays(self):
        a = self.queue.submit_data_only("a", digest("a"))
        b = self.queue.submit_data_only("b", digest("b"))
        active = self.queue.take_next_data_only()
        self.assertEqual(active.queue_id, a.queue_id)
        self.queue.close_data_only()
        self.queue.close_data_only()
        for name, item in (("a", a), ("b", b)):
            result = self.queue.observe_data_only(name, digest(name))
            self.assertEqual(result.status, "UNKNOWN_DATA_ONLY")
            self.assertEqual(result.queue_id, item.queue_id)
        self.assertEqual(self.queue.submit_data_only(
            "c", digest("c")).status, "DENY_CLOSED")
        self.assertIsNone(self.queue.take_next_data_only())
        self.assertEqual(self.queue.complete_data_only(
            active.queue_id, "REVIEWED_DATA_ONLY").status, "DENY_NO_ACTIVE")

    def test_independent_instances_are_not_cross_process_synchronization(self):
        a = OfflineReviewQueue()
        b = OfflineReviewQueue()
        first = a.submit_data_only("same", digest("same"))
        other = b.submit_data_only("same", digest("same"))
        self.assertNotEqual(first.queue_id, other.queue_id)
        self.assertEqual(a.take_next_data_only().status, "IN_REVIEW_DATA_ONLY")
        self.assertEqual(b.take_next_data_only().status, "IN_REVIEW_DATA_ONLY")
        # This limitation is a test requirement: only ONE protected Broker
        # instance may own the real queue; no global Python lock is security.


if __name__ == "__main__":
    unittest.main()
