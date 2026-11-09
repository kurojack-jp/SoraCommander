"""CORE only: bounded raw snapshots, strict Broker validation, temp children."""
import base64
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from sora_commander_next.broker_contract import BoundRequest
from sora_commander_next.broker_runtime import BrokerRuntime
from sora_commander_next.jobs import JobManager, UnknownSession


RID, SID = "a" * 32, "c" * 48
PAGE_FIELDS = {
    "request_id", "session_id", "status", "exit_code", "offset",
    "next_offset", "captured_bytes", "output_incomplete", "overflow",
    "end_of_capture", "stdout_base64", "stderr_base64",
}


class Guard:
    def __init__(self):
        self.allowed = True
        self.calls = 0

    def verify(self):
        self.calls += 1
        if not self.allowed:
            raise PermissionError("Owner locked")


def page():
    return {
        "request_id": RID, "session_id": SID, "status": "COMPLETED",
        "exit_code": 0, "offset": 0, "next_offset": 3, "captured_bytes": 3,
        "output_incomplete": False, "overflow": False, "end_of_capture": True,
        "stdout_base64": "AP+A", "stderr_base64": "",
    }


class LivePageValidationTests(unittest.TestCase):
    def setUp(self):
        self.guard = Guard()
        self.manager = Mock()
        self.manager.read_live_page.return_value = page()
        self.runtime = BrokerRuntime(
            expected_executable=sys.executable, expected_cwd=str(Path.cwd()),
            owner_binding="owner", owner_guard=self.guard, clock=lambda: 100,
            manager=self.manager)
        # Model only the Broker's existing correlation; never admit via reads.
        self.runtime.sessions[RID] = SID

    def assert_unknown(self):
        self.assertEqual(self.runtime.query_result_page(RID),
                         {"request_id": RID, "status": "OUTCOME_UNKNOWN"})

    def test_valid_page_and_unknown_request_are_readonly(self):
        self.assertEqual(self.runtime.query_result_page(RID), page())
        self.assertEqual(self.guard.calls, 2)
        self.manager.read_live_page.assert_called_once_with(SID, 0)
        self.assertEqual(self.runtime.query_result_page("d" * 32),
                         {"request_id": "d" * 32, "status": "UNKNOWN"})
        self.assertEqual(self.guard.calls, 4)
        self.manager.read_live_page.assert_called_once()
        self.manager.start.assert_not_called()
        self.manager.read.assert_not_called()
        self.manager.stop.assert_not_called()

    def test_invalid_lookup_inputs_and_no_session_parameter(self):
        for offset in (True, False, -1, 65537, 2**100, 0.0, "0", None):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.runtime.query_result_page(RID, offset=offset)
        for rid in (None, True, [], "", "A" * 32, "g" * 32, "a" * 33):
            with self.subTest(rid=rid), self.assertRaises(ValueError):
                self.runtime.query_result_page(rid)
        with self.assertRaises(TypeError):
            self.runtime.query_result_page(RID, session_id=SID)
        self.manager.read_live_page.assert_not_called()

    def test_wrong_or_missing_ids_never_disclose_output(self):
        for key, value in (("request_id", "f" * 32), ("session_id", "f" * 48),
                           ("request_id", None), ("session_id", None)):
            with self.subTest(key=key, value=value):
                self.manager.read_live_page.return_value = {**page(), key: value}
                self.assert_unknown()
        self.manager.start.assert_not_called()

    def test_every_missing_field_and_additional_metadata_fail_closed(self):
        for key in PAGE_FIELDS:
            with self.subTest(missing=key):
                value = page()
                del value[key]
                self.manager.read_live_page.return_value = value
                self.assert_unknown()
        for key in ("pid", "argv", "cwd", "environment", "stdout", "chunks"):
            with self.subTest(extra=key):
                self.manager.read_live_page.return_value = {**page(), key: "private"}
                self.assert_unknown()
        for value in (None, [], "private", True):
            with self.subTest(shape=value):
                self.manager.read_live_page.return_value = value
                self.assert_unknown()

    def test_malformed_status_exit_offsets_sizes_flags_and_base64(self):
        changes = [
            {"status": s} for s in (None, [], True, "UNKNOWN", "CANCELLING", "")
        ] + [
            {"exit_code": c} for c in (None, True, 0.0, "0", 2**32, -(2**31)-1, 1)
        ] + [
            {"status": "RUNNING", "exit_code": 0},
            {"status": "FAILED", "exit_code": 0},
            {"status": "FAILED", "exit_code": None},
            {"offset": True}, {"offset": 1}, {"offset": -1},
            {"next_offset": True}, {"next_offset": 2}, {"next_offset": 4097},
            {"captured_bytes": True}, {"captured_bytes": -1},
            {"captured_bytes": 65537}, {"captured_bytes": 4},
            {"overflow": 1}, {"output_incomplete": None},
            {"end_of_capture": 1}, {"end_of_capture": False},
            {"stdout_base64": None}, {"stderr_base64": b""},
            {"stdout_base64": "!!!!"}, {"stdout_base64": "\u00e9"},
            {"stdout_base64": "AP+A\n"}, {"stdout_base64": "AP+A="},
            {"stdout_base64": "AB=="},  # noncanonical padding bits
            {"stdout_base64": "A" * 5465},
            {"stdout_base64": base64.b64encode(b"x"*4097).decode()},
            {"stdout_base64": base64.b64encode(b"x"*4096).decode(),
             "stderr_base64": "eA==", "captured_bytes": 4097, "next_offset": 4096,
             "end_of_capture": False},  # total across BOTH streams exceeds 4096
        ]
        for change in changes:
            with self.subTest(change=change):
                self.manager.read_live_page.return_value = {**page(), **change}
                self.assert_unknown()

    def test_running_and_adverse_terminal_states_are_preserved(self):
        for status, code in (("RUNNING", None), ("FAILED", 3), ("FAILED", 2**32-1),
                             ("CANCELLED", 1), ("TIMEOUT", 1), ("OUTPUT_LIMIT", 1),
                             ("OUTPUT_INCOMPLETE", 0), ("ERROR", None),
                             ("OUTCOME_UNKNOWN", None)):
            with self.subTest(status=status):
                value = {**page(), "status": status, "exit_code": code,
                         "output_incomplete": True, "overflow": True}
                self.manager.read_live_page.return_value = value
                self.assertEqual(self.runtime.query_result_page(RID), value)

    def test_nonzero_offset_and_maximum_empty_tail(self):
        value = {**page(), "offset": 4096, "next_offset": 4099,
                 "captured_bytes": 4099}
        self.manager.read_live_page.return_value = value
        self.assertEqual(self.runtime.query_result_page(RID, offset=4096), value)
        value = {**page(), "offset": 65536, "next_offset": 65536,
                 "captured_bytes": 65536, "stdout_base64": ""}
        self.manager.read_live_page.return_value = value
        self.assertEqual(self.runtime.query_result_page(RID, offset=65536), value)

    def test_manager_failure_and_changed_broker_correlation_fail_closed(self):
        for error in (UnknownSession(), ValueError(), OSError(), RuntimeError()):
            with self.subTest(error=type(error)):
                self.manager.read_live_page.side_effect = error
                self.assert_unknown()

        def changed(*args):
            self.runtime.sessions[RID] = "e" * 48
            return page()

        self.manager.read_live_page.side_effect = changed
        self.assert_unknown()

    def test_owner_lock_before_read_denies_without_manager_call(self):
        self.guard.allowed = False
        with self.assertRaises(PermissionError):
            self.runtime.query_result_page(RID)
        self.manager.read_live_page.assert_not_called()

    def test_owner_lock_during_read_denies_success_mismatch_and_errors(self):
        for outcome in (page(), {**page(), "request_id": "f"*32}, OSError()):
            with self.subTest(outcome=outcome):
                self.guard.allowed = True

                def lock(*args):
                    self.guard.allowed = False
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

                self.manager.read_live_page.side_effect = lock
                with self.assertRaises(PermissionError):
                    self.runtime.query_result_page(RID)

    def test_owner_check_follows_payload_validation(self):
        from unittest.mock import patch
        decode = base64.b64decode

        def lock_while_validating(*args, **kwargs):
            self.guard.allowed = False
            return decode(*args, **kwargs)

        with patch("sora_commander_next.broker_runtime.base64.b64decode",
                   side_effect=lock_while_validating):
            with self.assertRaises(PermissionError):
                self.runtime.query_result_page(RID)


class LivePageChildTests(unittest.TestCase):
    def test_real_binary_running_and_finished_pages_without_extra_spawn(self):
        # An ordinary disposable Windows Python child. No GUI, IPC or storage
        # configuration; the temp marker only lets this child finish normally.
        with tempfile.TemporaryDirectory(prefix="next-live-pages-") as tmp:
            guard = Guard()
            manager = JobManager(max_concurrent=1, capacity=2)
            runtime = BrokerRuntime(
                expected_executable=sys.executable, expected_cwd=tmp,
                owner_binding="owner", owner_guard=guard, clock=lambda: 100,
                manager=manager)
            marker = Path(tmp) / "release"
            code = (
                "import sys,time;from pathlib import Path;"
                "sys.stdout.buffer.write((bytes(range(256))*18)[:4500]);"
                "sys.stdout.flush();"
                "sys.stderr.buffer.write((bytes(range(255,-1,-1))*3)[:700]);"
                "sys.stderr.flush();deadline=time.monotonic()+6\n"
                "while not Path('release').exists() and time.monotonic()<deadline:\n"
                " time.sleep(.01)\n"
                "sys.stderr.buffer.write(b'TAIL\\x00\\xfe');sys.stderr.flush()"
            )
            request = BoundRequest(RID, sys.executable, ("-I", "-S", "-u", "-c", code),
                                   tmp, "b"*64, "owner", 1000, 10, 8192)
            manager.start = Mock(wraps=manager.start)
            manager.read = Mock(side_effect=AssertionError("legacy read forbidden"))
            try:
                runtime.start_approved(request, approved_fingerprint=request.fingerprint(),
                                       current_task_sha256=request.task_sha256)
                sid = runtime.sessions[RID]
                deadline = time.monotonic() + 4
                while True:
                    first = runtime.query_result_page(RID)
                    if first.get("captured_bytes") == 5200:
                        break
                    self.assertLess(time.monotonic(), deadline, first)
                    time.sleep(.01)
                self.assertEqual(first["status"], "RUNNING")
                self.assertEqual((first["offset"], first["next_offset"]), (0, 4096))
                self.assertFalse(first["end_of_capture"])
                second = runtime.query_result_page(RID, offset=4096)
                self.assertEqual(second["next_offset"], 5200)
                self.assertTrue(second["end_of_capture"])
                self.assertEqual(second["status"], "RUNNING")
                empty = runtime.query_result_page(RID, offset=5200)
                self.assertEqual((empty["stdout_base64"], empty["stderr_base64"]), ("", ""))
                marker.touch()
                self.assertTrue(manager._jobs[sid].done.wait(5))
                tail = runtime.query_result_page(RID, offset=5200)
                self.assertEqual((tail["status"], tail["exit_code"]), ("COMPLETED", 0))
                self.assertEqual(tail["next_offset"], 5206)
                for stream, expected in (
                    ("stdout", (bytes(range(256))*18)[:4500]),
                    ("stderr", (bytes(range(255,-1,-1))*3)[:700] + b"TAIL\x00\xfe"),
                ):
                    recovered = b"".join(base64.b64decode(p[stream+"_base64"], validate=True)
                                         for p in (first, second, tail))
                    self.assertEqual(recovered, expected)
                for p in (first, second, empty, tail):
                    self.assertEqual(set(p), PAGE_FIELDS)
                    size = sum(len(base64.b64decode(p[s+"_base64"]))
                               for s in ("stdout", "stderr"))
                    self.assertLessEqual(size, 4096)
                    self.assertEqual(size, p["next_offset"] - p["offset"])
                    self.assertFalse(p["output_incomplete"])
                    self.assertFalse(p["overflow"])
                self.assertEqual(runtime.query_result_page(RID, offset=5200), tail)
                self.assertTrue(runtime.query_result_page(RID, offset=5206)["end_of_capture"])
                for offset in (True, -1, 65537, 2**100, "0", 5207):
                    with self.subTest(offset=offset), self.assertRaises(ValueError):
                        manager.read_live_page(sid, offset)
                with self.assertRaises(UnknownSession):
                    manager.read_live_page("f" * 48)
                with self.assertRaises(ValueError):
                    manager.read_live_page("bad")
                self.assertEqual(runtime.query_result_page(RID, offset=5207),
                                 {"request_id": RID, "status": "OUTCOME_UNKNOWN"})
                guard.allowed = False
                with self.assertRaises(PermissionError):
                    runtime.query_result_page(RID)
                manager.start.assert_called_once()
                manager.read.assert_not_called()
                self.assertEqual(len(manager._jobs), 1)
            finally:
                try:
                    marker.touch()
                finally:
                    runtime.close()

    def test_empty_and_maximum_capture_boundaries_and_adverse_flags(self):
        # Synthetic raw chunks exercise exact byte limits independently of
        # OS scheduling. Real subprocess behavior is checked above.
        from types import SimpleNamespace
        from threading import RLock
        manager = JobManager()
        try:
            job = SimpleNamespace(
                lock=RLock(), request=SimpleNamespace(request_id=RID), chunks=[],
                captured=0, status="RUNNING", exit_code=None,
                overflow=False, output_incomplete=False)
            manager._lookup = Mock(return_value=job)
            empty = manager.read_live_page(SID)
            self.assertEqual(empty["next_offset"], 0)
            self.assertTrue(empty["end_of_capture"])
            job.chunks = [(i, "stdout" if i % 2 else "stderr", bytes([i % 256]))
                          for i in range(65536)]
            job.captured = 65536
            job.status, job.exit_code = "OUTPUT_LIMIT", 1
            job.overflow = job.output_incomplete = True
            first = manager.read_live_page(SID)
            self.assertEqual(first["next_offset"], 4096)
            self.assertEqual(len(base64.b64decode(first["stdout_base64"])), 2048)
            self.assertEqual(len(base64.b64decode(first["stderr_base64"])), 2048)
            last = manager.read_live_page(SID, 65535)
            self.assertEqual(last["next_offset"], 65536)
            self.assertEqual(base64.b64decode(last["stdout_base64"]), b"\xff")
            self.assertTrue(last["overflow"])
            self.assertTrue(last["output_incomplete"])
            self.assertEqual(last["status"], "OUTPUT_LIMIT")
            self.assertTrue(manager.read_live_page(SID, 65536)["end_of_capture"])
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
