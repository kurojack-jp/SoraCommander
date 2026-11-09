"""DEV-only locally owned result-wire adapter tests; no actual IPC."""
import base64
import json
import unittest

from sora_commander_next.broker_contract import BoundRequest
from sora_commander_next.broker_runtime import BrokerRuntime
from sora_commander_next.offline_broker_result_reader import OfflineLocalResultReader
from sora_commander_next.offline_broker_result_wire import (
    ResultWireRejected, decode_read_result, MAX_RESULT_FRAME,
)


class Guard:
    enabled = True
    def verify(self):
        if not self.enabled:
            raise PermissionError("Owner state changed")
        return True


class Manager:
    def __init__(self):
        self.data = [(0, "stdout", b"A" * 4500),
                     (4500, "stderr", b"B" * 700)]
        self.reads = 0
        self.starts = 0
        self.tamper = False

    def start(self, command):
        self.starts += 1
        return {"status": "RUNNING", "request_id": command.request_id,
                "session_id": "c" * 48}

    def read(self, sid, offset=0):
        self.reads += 1
        pieces = []
        for start, stream, data in self.data:
            if start + len(data) > offset:
                begin = max(start, offset)
                pieces.append({"offset": begin, "stream": stream,
                               "data_base64": base64.b64encode(
                                   data[begin - start:]).decode("ascii")})
        if self.tamper and pieces:
            pieces[0]["offset"] += 1
        return {
            "request_id": "a" * 32, "session_id": sid,
            "status": "COMPLETED", "exit_code": 0,
            "offset": offset, "next_offset": 5200,
            "captured_bytes": 5200, "output_incomplete": False,
            "overflow": False, "chunks": pieces,
        }

    def close(self):
        pass


class LocalResultReaderTests(unittest.TestCase):
    def setUp(self):
        self.guard = Guard()
        self.manager = Manager()
        self.request = BoundRequest(
            "a" * 32, "C:/fixed.exe", ("-File", "fixed.ps1"), "D:/fixture",
            "b" * 64, "owner", 1000.0, 5.0, 8192)
        self.runtime = BrokerRuntime(
            expected_executable=self.request.executable,
            expected_cwd=self.request.cwd, owner_binding="owner",
            owner_guard=self.guard, clock=lambda: 100.0,
            manager=self.manager)
        self.runtime.start_approved(
            self.request, approved_fingerprint=self.request.fingerprint(),
            current_task_sha256=self.request.task_sha256)
        self.reader = OfflineLocalResultReader(self.runtime)

    def req(self, offset=0, rid=None, sid=None, op="read_result"):
        return json.dumps({
            "version": 2, "op": op,
            "request_id": rid if rid is not None else "a" * 32,
            "session_id": sid if sid is not None else "c" * 48,
            "offset": offset}, separators=(",", ":")).encode("ascii")

    def test_real_byte_offsets_paginate_across_streams(self):
        a = self.reader.read_local(self.req())
        self.assertLessEqual(len(a), MAX_RESULT_FRAME)
        first = decode_read_result(a)
        self.assertEqual((first["offset"], first["next_offset"], first["captured_bytes"]),
                         (0, 4096, 5200))
        self.assertEqual(base64.b64decode(first["stdout_base64"]), b"A" * 4096)
        self.assertEqual(base64.b64decode(first["stderr_base64"]), b"")
        second = decode_read_result(self.reader.read_local(self.req(4096)))
        self.assertEqual((second["offset"], second["next_offset"]), (4096, 5200))
        self.assertEqual(base64.b64decode(second["stdout_base64"]), b"A" * 404)
        self.assertEqual(base64.b64decode(second["stderr_base64"]), b"B" * 700)
        terminal = decode_read_result(self.reader.read_local(self.req(5200)))
        self.assertEqual((terminal["offset"], terminal["next_offset"]), (5200, 5200))
        self.assertEqual(self.manager.starts, 1)

    def test_mismatched_id_never_reads(self):
        n = self.manager.reads
        for bad in (self.req(rid="d"*32), self.req(sid="e"*48)):
            with self.assertRaises(ResultWireRejected):
                self.reader.read_local(bad)
        self.assertEqual(self.manager.reads, n)
        self.assertEqual(self.manager.starts, 1)

    def test_invalid_and_privileged_ops_rejected(self):
        for raw in (self.req(op="start"), self.req(offset=-1),
                    self.req(offset=True), self.req(offset=65537),
                    b'{"version":2,"op":"read_result","request_id":"a"}',
                    self.req()[:-1] + b',"approve":true}'):
            with self.subTest(raw=raw):
                with self.assertRaises(ResultWireRejected):
                    self.reader.read_local(raw)
        self.assertEqual(self.manager.starts, 1)

    def test_owner_change_and_malformed_chunks_fail_closed(self):
        self.guard.enabled = False
        with self.assertRaises(PermissionError):
            self.reader.read_local(self.req())
        self.guard.enabled = True
        self.manager.tamper = True
        with self.assertRaises(ResultWireRejected):
            self.reader.read_local(self.req())


if __name__ == "__main__":
    unittest.main()
