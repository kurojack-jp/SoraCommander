"""CORE v2 codec/local adapter; mock Owner checks and disposable OS child only."""
import base64
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sora_commander_next.broker_contract import BoundRequest
from sora_commander_next.broker_dispatch import BrokerDispatcher
from sora_commander_next.broker_protocol import (
    MAX_FRAME, OPERATIONS, ProtocolRejected, decode_request,
    decode_result_page_request, encode_response, encode_result_page_response,
)
from sora_commander_next.broker_runtime import BrokerRuntime
from sora_commander_next.jobs import JobManager


RID, SID = "a" * 32, "c" * 48


def request(**changes):
    return json.dumps({"version": 2, "op": "read_result", "request_id": RID,
                       "offset": 0, **changes}, separators=(",", ":")).encode()


def page(**changes):
    return {"request_id": RID, "session_id": SID, "status": "COMPLETED",
            "exit_code": 0, "offset": 0, "next_offset": 3, "captured_bytes": 3,
            "output_incomplete": False, "overflow": False, "end_of_capture": True,
            "stdout_base64": "AP+A", "stderr_base64": "", **changes}


class Guard:
    def __init__(self):
        self.allowed = True
        self.calls = 0

    def verify(self):
        self.calls += 1
        if not self.allowed:
            raise PermissionError("Owner locked")


class ResultCodecTests(unittest.TestCase):
    def test_request_boundaries_and_exact_frame_limit(self):
        for offset in (0, 1, 4096, 65536):
            raw = request(offset=offset)
            expected = json.loads(raw)
            self.assertEqual(decode_result_page_request(raw), expected)
            padded = raw + b" " * (MAX_FRAME - len(raw))
            self.assertEqual(len(padded), 8192)
            self.assertEqual(decode_result_page_request(padded), expected)
            with self.assertRaises(ProtocolRejected):
                decode_result_page_request(padded + b" ")

    def test_request_json_fuzz_is_rejected(self):
        cases = [None, "{}", bytearray(request()), b"", b"x" * 8193,
                 b"\xff", b"\xef\xbb\xbf" + request(), b"null", b"[]", b"true",
                 request() + b"{}", request() + b"\x00", request()[:-1],
                 b"[" * 3000 + b"]" * 3000]
        for token in (b"NaN", b"Infinity", b"-Infinity", b"1e999"):
            cases.append(request().replace(b'"offset":0', b'"offset":' + token))
        for key, value in (("version", 2), ("op", "read_result"),
                           ("request_id", RID), ("offset", 0)):
            cases.append(request()[:-1] + b"," + json.dumps(key).encode()
                         + b":" + json.dumps(value).encode() + b"}")
        cases.append(request()[:-1] + b',"off\\u0073et":0}')
        for raw in cases:
            with self.subTest(raw=str(raw)[:90]), self.assertRaises(ProtocolRejected):
                decode_result_page_request(raw)

    def test_request_strict_types_fields_and_operations(self):
        changes = [{"version": v} for v in (1, 3, True, 2.0, "2", None)]
        changes += [{"offset": v} for v in (True, False, -1, 65537, 0.0, "0", None, [])]
        changes += [{"request_id": v} for v in ("A"*32, "g"*32, "a"*31,
                                               "a"*33, RID+"\n", None, [], 1)]
        changes += [{"op": v} for v in ("execute", "submit", "retry", "approve",
                                        "cancel", "query", "health", None, [])]
        changes += [{key: "forged"} for key in ("session_id", "credential", "command",
                                                "argv", "path", "status", "approval")]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ProtocolRejected):
                decode_result_page_request(request(**change))
        for key in json.loads(request()):
            data = json.loads(request())
            del data[key]
            with self.subTest(missing=key), self.assertRaises(ProtocolRejected):
                decode_result_page_request(json.dumps(data).encode())

    def test_response_preserves_exact_page_and_is_canonical(self):
        data = page()
        raw = encode_result_page_response(data)
        expected = {"version": 2, "op": "read_result", **data}
        self.assertEqual(json.loads(raw), expected)
        self.assertEqual(raw, json.dumps(expected, sort_keys=True,
                                        separators=(",", ":")).encode())
        self.assertEqual(raw, encode_result_page_response(dict(reversed(list(data.items())))))
        self.assertLessEqual(len(raw), 8192)
        self.assertEqual(data, page())

    def test_response_only_unknown_states_may_omit_page(self):
        for state in ("UNKNOWN", "OUTCOME_UNKNOWN"):
            data = {"request_id": RID, "status": state}
            result = json.loads(encode_result_page_response(data))
            self.assertEqual(result, {"version": 2, "op": "read_result", **data})
            self.assertLess(len(encode_result_page_response(data)), 160)
        result = json.loads(encode_result_page_response(page(status="OUTCOME_UNKNOWN")))
        self.assertEqual(set(result), {"version", "op", "request_id", "status"})
        for state in ("COMPLETED", "RUNNING", "OK", "APPROVED", "ERROR", None, []):
            with self.subTest(state=state), self.assertRaises(ProtocolRejected):
                encode_result_page_response({"request_id": RID, "status": state})

    def test_response_missing_extra_and_unexpected_shapes_rejected(self):
        cases = [None, [], True, "private", {**page(), "command": "private"},
                 {**page(), "version": 2}, {**page(), "op": "read_result"}]
        for key in page():
            data = page()
            del data[key]
            cases.append(data)
        cases.append({"request_id": RID, "status": "UNKNOWN", "session_id": SID})
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ProtocolRejected):
                encode_result_page_response(data)

    def test_response_strict_scalar_types_and_status(self):
        changes = [{"request_id": x} for x in (None, [], "A"*32, "f"*33)]
        changes += [{"session_id": x} for x in (None, [], "f"*47, "F"*48, "../")]
        changes += [{"status": x} for x in (None, [], True, "UNKNOWN", "OK", "APPROVED")]
        changes += [{"exit_code": x} for x in (None, True, False, 0.0, "0",
                                               -(2**31)-1, 2**32, 1)]
        changes += [{"status": "RUNNING", "exit_code": 0},
                    {"status": "FAILED", "exit_code": None},
                    {"status": "FAILED", "exit_code": 0}]
        changes += [{key: x} for key in ("offset", "next_offset", "captured_bytes")
                    for x in (True, False, -1, 65537, 0.0, None, "0")]
        changes += [{"offset": 4}, {"next_offset": 2}, {"captured_bytes": 4}]
        changes += [{key: x} for key in ("overflow", "output_incomplete", "end_of_capture")
                    for x in (0, 1, None, "false")]
        changes += [{"end_of_capture": False}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ProtocolRejected):
                encode_result_page_response(page(**change))

    def test_response_bad_base64_and_combined_binary_cap(self):
        for key in ("stdout_base64", "stderr_base64"):
            for value in (None, b"", "!!!!", "\u00e9", "AP+A\n", "AP+A=", "AB==",
                          "A"*5465, base64.b64encode(b"x"*4097).decode()):
                with self.subTest(key=key, value=str(value)[:40]), self.assertRaises(ProtocolRejected):
                    encode_result_page_response(page(**{key: value}))
        with self.assertRaises(ProtocolRejected):
            encode_result_page_response(page(
                stdout_base64=base64.b64encode(b"x"*4096).decode(),
                stderr_base64="eA==", captured_bytes=4097, next_offset=4096,
                end_of_capture=False))

    def test_response_full_page_and_empty_maximum_offset(self):
        for stdout_size in (0, 1, 2048, 4095, 4096):
            data = page(stdout_base64=base64.b64encode(b"\xff"*stdout_size).decode(),
                        stderr_base64=base64.b64encode(b"\x00"*(4096-stdout_size)).decode(),
                        next_offset=4096, captured_bytes=65536, end_of_capture=False)
            raw = encode_result_page_response(data)
            self.assertLessEqual(len(raw), 8192)
            self.assertEqual(json.loads(raw), {"version": 2, "op": "read_result", **data})
        data = page(offset=65536, next_offset=65536, captured_bytes=65536, stdout_base64="")
        self.assertEqual(json.loads(encode_result_page_response(data))["offset"], 65536)

    def test_response_adverse_states_preserve_data_not_authority(self):
        for state, code in (("RUNNING", None), ("FAILED", 3), ("FAILED", -(2**31)),
                            ("FAILED", 2**32-1), ("CANCELLED", 1), ("TIMEOUT", 1),
                            ("OUTPUT_LIMIT", 1), ("OUTPUT_INCOMPLETE", 0), ("ERROR", None)):
            data = page(status=state, exit_code=code, output_incomplete=True, overflow=True)
            self.assertEqual(json.loads(encode_result_page_response(data)),
                             {"version": 2, "op": "read_result", **data})

    def test_v1_exports_schema_and_dispatch_stay_separate(self):
        self.assertEqual(OPERATIONS, frozenset(("health", "query", "cancel")))
        self.assertEqual(MAX_FRAME, 8192)
        for op in OPERATIONS:
            data = {"version": 1, "op": op}
            if op != "health":
                data["request_id"] = RID
            self.assertEqual(decode_request(json.dumps(data).encode()), data)
            with self.assertRaises(ProtocolRejected):
                decode_result_page_request(json.dumps(data).encode())
        for raw in (request(), request(version=1)):
            with self.assertRaises(ProtocolRejected):
                decode_request(raw)
        data = {"version": 1, "status": "OK", "request_id": RID}
        self.assertEqual(encode_response(data), json.dumps(data, separators=(",", ":")).encode())
        with self.assertRaises(ProtocolRejected):
            encode_response({**data, "op": "read_result"})
        lookup, cancel = Mock(), Mock()
        dispatcher = BrokerDispatcher(owner_guard=Guard(), lookup=lookup, cancel=cancel)
        self.assertEqual(json.loads(dispatcher.handle(request())),
                         {"version": 1, "status": "DENIED", "request_id": None})
        lookup.assert_not_called()
        cancel.assert_not_called()


class ResultLocalAdapterTests(unittest.TestCase):
    def setUp(self):
        self.guard = Guard()
        self.manager = Mock()
        self.manager.read_live_page.return_value = page()
        self.runtime = BrokerRuntime(
            expected_executable=sys.executable, expected_cwd=str(Path.cwd()),
            owner_binding="owner", owner_guard=self.guard, clock=lambda: 100,
            manager=self.manager)
        self.runtime.sessions[RID] = SID

    def read(self, **changes):
        return self.runtime.read_result_page_wire_local(request(**changes))

    def test_valid_read_uses_guarded_page_without_admission_or_mutation(self):
        with patch.object(self.runtime, "query_result_page",
                          wraps=self.runtime.query_result_page) as query:
            raw = self.read()
            query.assert_called_once_with(RID, offset=0)
        self.assertEqual(json.loads(raw), {"version": 2, "op": "read_result", **page()})
        self.assertEqual(self.guard.calls, 4)
        self.assertEqual(self.runtime.admission._accepted, {})
        self.assertEqual(self.manager.mock_calls,
                         [unittest.mock.call.read_live_page(SID, 0)])

    def test_unknown_request_is_small_and_does_not_read_manager(self):
        raw = self.read(request_id="f"*32)
        self.assertEqual(json.loads(raw), {"version": 2, "op": "read_result",
                                          "request_id": "f"*32, "status": "UNKNOWN"})
        self.manager.read_live_page.assert_not_called()

    def test_cross_request_session_malformed_and_extra_manager_data_do_not_leak(self):
        cases = [None, [], {**page(), "request_id": "f"*32},
                 {**page(), "session_id": "e"*48}, {**page(), "argv": "private"},
                 page(stdout_base64="AB=="), page(next_offset=2)]
        for data in cases:
            with self.subTest(data=data):
                self.manager.read_live_page.return_value = data
                self.assertEqual(json.loads(self.read()), {
                    "version": 2, "op": "read_result", "request_id": RID,
                    "status": "OUTCOME_UNKNOWN"})

    def test_changed_correlation_during_manager_read_does_not_leak(self):
        def changed(*args):
            self.runtime.sessions[RID] = "e"*48
            return page()
        self.manager.read_live_page.side_effect = changed
        self.assertEqual(json.loads(self.read())["status"], "OUTCOME_UNKNOWN")

    def test_manager_failure_is_small_unknown(self):
        self.manager.read_live_page.side_effect = OSError("private")
        self.assertEqual(json.loads(self.read()), {"version": 2, "op": "read_result",
                                                  "request_id": RID, "status": "OUTCOME_UNKNOWN"})

    def test_malformed_request_refused_without_manager_call_or_log(self):
        with patch("sys.stdout") as stdout, patch("sys.stderr") as stderr:
            for raw in (request(session_id=SID), request(offset=True), b"private", b""):
                with self.subTest(raw=raw), self.assertRaises(ProtocolRejected):
                    self.runtime.read_result_page_wire_local(raw)
            self.assertEqual(stdout.mock_calls, [])
            self.assertEqual(stderr.mock_calls, [])
        self.manager.read_live_page.assert_not_called()

    def test_owner_locked_before_read_returns_no_bytes(self):
        self.guard.allowed = False
        with self.assertRaises(PermissionError):
            self.read()
        self.manager.read_live_page.assert_not_called()

    def test_owner_lock_during_query_including_manager_failure_returns_no_bytes(self):
        for result in (page(), None, OSError("private")):
            self.guard.allowed = True
            def lock(*args):
                self.guard.allowed = False
                if isinstance(result, Exception):
                    raise result
                return result
            self.manager.read_live_page.side_effect = lock
            returned = []
            with self.subTest(result=result), self.assertRaises(PermissionError):
                returned.append(self.read())
            self.assertEqual(returned, [])

    def test_owner_lock_after_query_returns_no_bytes(self):
        query = self.runtime.query_result_page
        def lock(*args, **kwargs):
            result = query(*args, **kwargs)
            self.guard.allowed = False
            return result
        with patch.object(self.runtime, "query_result_page", side_effect=lock):
            with self.assertRaises(PermissionError):
                self.read()

    def test_owner_lock_after_encoding_including_unknown_and_error_returns_no_bytes(self):
        for manager_error in (False, True):
            for codec_error in (False, True):
                self.guard.allowed = True
                self.manager.read_live_page.side_effect = OSError() if manager_error else None
                def lock(data):
                    raw = encode_result_page_response(data)
                    self.guard.allowed = False
                    if codec_error:
                        raise ProtocolRejected("serialization failed")
                    return raw
                returned = []
                with self.subTest(manager_error=manager_error, codec_error=codec_error):
                    with patch("sora_commander_next.broker_runtime.encode_result_page_response",
                               side_effect=lock), self.assertRaises(PermissionError):
                        returned.append(self.read())
                    self.assertEqual(returned, [])

    def test_unexpected_query_and_codec_errors_are_refused_with_final_guard(self):
        for result in (None, {}, {"request_id": RID, "status": "COMPLETED"}):
            before = self.guard.calls
            with patch.object(self.runtime, "query_result_page", return_value=result):
                with self.assertRaises(ProtocolRejected):
                    self.read()
            self.assertEqual(self.guard.calls - before, 2)
        before = self.guard.calls
        with patch.object(self.runtime, "query_result_page", side_effect=RuntimeError("private")):
            with self.assertRaises(RuntimeError):
                self.read()
        self.assertEqual(self.guard.calls - before, 2)


class ResultWireChildTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows disposable-child acceptance")
    def test_binary_stdout_stderr_roundtrip_multiple_pages_one_launch(self):
        # No Owner UI or IPC. One finite child writes bytes and exits normally.
        guard = Guard()
        manager = JobManager(max_concurrent=1, capacity=2)
        cwd = str(Path.cwd())
        runtime = BrokerRuntime(expected_executable=sys.executable, expected_cwd=cwd,
                                owner_binding="owner", owner_guard=guard,
                                clock=lambda: 100, manager=manager)
        out = bytes(range(256))*20
        err = bytes(range(255, -1, -1))*19
        code = ("import sys;sys.stdout.buffer.write(bytes(range(256))*20);"
                "sys.stdout.flush();"
                "sys.stderr.buffer.write(bytes(range(255,-1,-1))*19);sys.stderr.flush()")
        req = BoundRequest(RID, sys.executable, ("-I", "-S", "-u", "-c", code),
                           cwd, "b"*64, "owner", 1000, 10, 16384)
        manager.start = Mock(wraps=manager.start)
        manager.read = Mock(side_effect=AssertionError("legacy read forbidden"))
        try:
            started = runtime.start_approved(req, approved_fingerprint=req.fingerprint(),
                                             current_task_sha256=req.task_sha256)
            self.assertEqual(started["status"], "RUNNING")
            sid = runtime.sessions[RID]
            self.assertTrue(manager._jobs[sid].done.wait(6))
            streams = {"stdout": bytearray(), "stderr": bytearray()}
            offset, pages = 0, 0
            while True:
                wire = runtime.read_result_page_wire_local(request(offset=offset))
                result = json.loads(wire)
                self.assertEqual(wire, json.dumps(result, sort_keys=True,
                                                  separators=(",", ":")).encode())
                self.assertLessEqual(len(wire), 8192)
                self.assertEqual(set(result), set(page()) | {"version", "op"})
                self.assertEqual((result["version"], result["op"]), (2, "read_result"))
                self.assertEqual((result["request_id"], result["session_id"]), (RID, sid))
                self.assertEqual((result["status"], result["exit_code"]), ("COMPLETED", 0))
                self.assertFalse(result["overflow"])
                self.assertFalse(result["output_incomplete"])
                self.assertEqual(result["offset"], offset)
                self.assertEqual(result["captured_bytes"], len(out)+len(err))
                size = 0
                for stream in streams:
                    binary = base64.b64decode(result[stream+"_base64"], validate=True)
                    streams[stream].extend(binary)
                    size += len(binary)
                self.assertLessEqual(size, 4096)
                self.assertEqual(size, result["next_offset"] - offset)
                self.assertEqual(result["next_offset"], min(offset+4096, len(out)+len(err)))
                self.assertEqual(result["end_of_capture"], result["next_offset"] == len(out)+len(err))
                pages += 1
                offset = result["next_offset"]
                if result["end_of_capture"]:
                    break
                self.assertLess(pages, 4)
            self.assertEqual(pages, 3)
            self.assertEqual(streams["stdout"], out)
            self.assertEqual(streams["stderr"], err)
            self.assertEqual(runtime.read_result_page_wire_local(request(offset=0)),
                             runtime.read_result_page_wire_local(request(offset=0)))
            empty = json.loads(runtime.read_result_page_wire_local(request(offset=offset)))
            self.assertEqual((empty["stdout_base64"], empty["stderr_base64"]), ("", ""))
            self.assertEqual(empty["offset"], empty["next_offset"])
            manager.start.assert_called_once()
            manager.read.assert_not_called()
            self.assertEqual(len(manager._jobs), 1)
            self.assertEqual(len(runtime.admission._accepted), 1)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
