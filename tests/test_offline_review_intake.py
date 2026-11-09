"""Inert Broker-intake tests, including two real Windows client processes.

The clients communicate over anonymous subprocess pipes that the TEST HARNESS
forwards into one OfflineReviewIntake. This is NOT an authenticated Windows
named-pipe listener, actual Owner GUI, grant issuer, or JobManager admission.
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.offline_review_intake import OfflineReviewIntake
from sora_commander_next.offline_review_queue import OfflineReviewQueue


def request(op="submit", *, rid="a" * 32, scope=None):
    content = {"version": 1, "op": op}
    if op != "health":
        content["request_id"] = rid
        content["scope_sha256"] = scope or sha256(b"mock-command").hexdigest()
    return json.dumps(content, separators=(",", ":")).encode()


def response(data):
    return json.loads(data.decode("ascii"))


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.queue = OfflineReviewQueue(pending_capacity=4,
                                        history_capacity=8)
        self.intake = OfflineReviewIntake(self.queue)

    def test_health_is_inert_and_no_control_operation(self):
        status = response(self.intake.handle_data_only(request("health")))
        self.assertEqual(status, {
            "version": 1, "status": "OK_DATA_ONLY", "request_id": None})
        for op in ("approve", "review", "complete", "revoke", "start", "cancel"):
            payload = request(op)
            self.assertEqual(response(self.intake.handle_data_only(
                payload))["status"], "DENY_FRAME")
        self.assertIsNone(self.queue.take_next_data_only())

    def test_same_request_dedup_and_read_only_status(self):
        frame = request()
        first = response(self.intake.handle_data_only(frame))
        second = response(self.intake.handle_data_only(frame))
        observed = response(self.intake.handle_data_only(request("observe")))
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "QUEUED_DATA_ONLY")
        self.assertEqual(observed["status"], "QUEUED_DATA_ONLY")
        self.assertNotIn("queue_id", first)
        self.assertEqual(self.queue.take_next_data_only().status,
                         "IN_REVIEW_DATA_ONLY")
        self.assertEqual(response(self.intake.handle_data_only(
            request("observe")))["status"], "IN_REVIEW_DATA_ONLY")

    def test_changed_scope_same_id_conflicts_without_second_ticket(self):
        frame = request()
        self.intake.handle_data_only(frame)
        altered = request(scope="f" * 64)
        self.assertEqual(response(self.intake.handle_data_only(
            altered))["status"], "DENY_CONFLICT")
        self.assertEqual(self.queue.take_next_data_only().request_id,
                         "a" * 32)
        self.assertIsNone(self.queue.take_next_data_only())

    def test_framing_rejects_untrusted_or_forged_fields(self):
        malformed = (
            b"", b"{}", b"null", b"[]", b"\xff",
            b'{"version":true,"op":"health"}',
            b'{"version":1,"op":"health","op":"submit"}',
            b'{"version":1,"op":"health","owner":true}',
            b'{"version":1,"op":"submit","request_id":"a","scope_sha256":"'
                + b"a" * 64 + b'"}',
            b'{"version":1,"op":"submit","request_id":"'
                + b"a" * 32 + b'","scope_sha256":"' + b"a" * 64
                + b'","approved":true}',
            b'{"version":NaN,"op":"health"}',
            b"x" * 2049,
        )
        for item in malformed:
            with self.subTest(item=item[:55]):
                self.assertEqual(response(self.intake.handle_data_only(
                    item))["status"], "DENY_FRAME")
        self.assertIsNone(self.queue.take_next_data_only())

    def test_only_owning_loop_can_complete_and_client_cannot(self):
        q = self.queue
        raw = request()
        self.intake.handle_data_only(raw)
        active = q.take_next_data_only()
        forged = {"version": 1, "op": "complete", "request_id": "a" * 32,
                  "queue_id": active.queue_id, "result": "REVIEWED_DATA_ONLY"}
        self.assertEqual(response(self.intake.handle_data_only(
            json.dumps(forged).encode()))["status"], "DENY_FRAME")
        self.assertEqual(q.observe_data_only(
            "a" * 32, sha256(b"mock-command").hexdigest()).status,
            "IN_REVIEW_DATA_ONLY")
        self.assertEqual(q.complete_data_only(
            active.queue_id, "DENIED_DATA_ONLY").status, "DENIED_DATA_ONLY")
        self.assertEqual(response(self.intake.handle_data_only(
            request("observe")))["status"], "DENIED_DATA_ONLY")

    def test_concurrent_client_threads_share_one_queue(self):
        frame = request()
        with ThreadPoolExecutor(max_workers=12) as workers:
            items = list(workers.map(
                self.intake.handle_data_only, [frame] * 40))
        self.assertEqual(
            {response(item)["status"] for item in items},
            {"QUEUED_DATA_ONLY"})
        self.assertEqual(self.queue.take_next_data_only().request_id,
                         "a" * 32)
        self.assertIsNone(self.queue.take_next_data_only())

    def test_two_separate_os_clients_forward_to_one_inert_broker(self):
        # Separate Python OS processes produce frames on anonymous stdout
        # pipes. The parent is the only intake owner and forwards both.
        # No Windows named pipe, remote transport or OS token auth is claimed.
        source = (
            "import sys;"
            "data=sys.stdin.buffer.read();"
            "sys.stdout.buffer.write(data);"
            "sys.stdout.buffer.flush()"
        )
        payload = request()
        processes = [
            subprocess.Popen(
                [sys.executable, "-I", "-B", "-c", source],
                cwd=str(ROOT), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(2)
        ]
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(proc.communicate, input=payload,
                                       timeout=8) for proc in processes]
                returned = [future.result() for future in futures]
            for process, (stdout, stderr) in zip(processes, returned):
                self.assertEqual(process.returncode, 0, stderr.decode(
                    errors="replace")[-300:])
                self.assertEqual(stdout, payload)
            # Two different process outputs reach ONE owner queue.
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(
                    self.intake.handle_data_only,
                    [pair[0] for pair in returned]))
            self.assertEqual({response(raw)["status"] for raw in responses},
                             {"QUEUED_DATA_ONLY"})
            self.assertEqual(self.queue.take_next_data_only().request_id,
                             "a" * 32)
            self.assertIsNone(self.queue.take_next_data_only())
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)

    def test_unexpected_queue_exception_is_bounded_and_no_private_text(self):
        with patch.object(self.queue, "submit_data_only",
                          side_effect=RuntimeError("sensitive internal path")):
            raw = self.intake.handle_data_only(request())
        self.assertEqual(response(raw), {
            "version": 1, "status": "ERROR_DATA_ONLY", "request_id": None})
        self.assertNotIn(b"sensitive", raw)
        self.assertIsNone(self.queue.take_next_data_only())

    def test_closed_queue_refuses_submissions(self):
        self.queue.close_data_only()
        self.assertEqual(response(self.intake.handle_data_only(
            request()))["status"], "DENY_CLOSED")
        self.assertIsNone(self.queue.take_next_data_only())


if __name__ == "__main__":
    unittest.main()
