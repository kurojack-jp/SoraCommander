"""Offline integration: strict wire parser -> authenticated dispatcher -> owned runtime."""
import json
import unittest
from sora_commander_next.broker_contract import BoundRequest
from sora_commander_next.broker_dispatch import BrokerDispatcher
from sora_commander_next.broker_runtime import BrokerRuntime


class Guard:
    allowed = True
    def verify(self):
        if not self.allowed:
            raise PermissionError("not Owner")


class Manager:
    def __init__(self):
        self.started = 0
        self.cancelled = 0
    def start(self, command):
        self.started += 1
        return {"status": "RUNNING", "request_id": command.request_id,
                "session_id": "b"*48}
    def request_status_readonly(self, request_id):
        return {"status": "RUNNING", "request_id": request_id,
                "session_id": "b"*48, "exit_code": None,
                "captured_bytes": 0, "output_incomplete": False}
    def stop(self, session):
        self.cancelled += 1
        # BrokerRuntime now requires exact owned-session correlation.
        return {"session_id": session, "status": "CANCELLING"}
    def close(self):
        pass


class BrokerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.guard = Guard()
        self.manager = Manager()
        self.req = BoundRequest("a"*32, "C:/fixed.exe", (), "D:/repo", "b"*64,
                                "owner", 1000.0, 30.0, 8192)
        self.runtime = BrokerRuntime(expected_executable=self.req.executable,
                                     expected_cwd=self.req.cwd, owner_binding="owner",
                                     owner_guard=self.guard, clock=lambda: 100.0,
                                     manager=self.manager)
        self.dispatcher = self.runtime.make_local_dispatcher()

    def wire(self, op, **kwargs):
        return json.loads(self.dispatcher.handle(json.dumps(
            {"version": 1, "op": op, **kwargs}).encode()))

    def test_approved_then_query_cancel(self):
        self.assertEqual(self.wire("query", request_id=self.req.request_id)["status"], "UNKNOWN")
        self.runtime.start_approved(self.req, approved_fingerprint=self.req.fingerprint(),
                                    current_task_sha256=self.req.task_sha256)
        self.assertEqual(self.wire("query", request_id=self.req.request_id)["status"], "OK")
        self.assertEqual(self.wire("cancel", request_id=self.req.request_id)["status"], "OK")
        self.assertEqual(self.manager.started, 1)
        self.assertEqual(self.manager.cancelled, 1)

    def test_wire_cannot_launch(self):
        self.assertEqual(self.wire("submit_approved", request_id=self.req.request_id)["status"], "DENIED")
        self.assertEqual(self.manager.started, 0)

    def test_owner_lock_denies_cancel(self):
        self.runtime.start_approved(self.req, approved_fingerprint=self.req.fingerprint(),
                                    current_task_sha256=self.req.task_sha256)
        self.guard.allowed = False
        self.assertEqual(self.wire("cancel", request_id=self.req.request_id)["status"], "DENIED")
        self.assertEqual(self.manager.cancelled, 0)

    def test_boolean_version_rejected(self):
        self.assertEqual(json.loads(self.dispatcher.handle(
            b'{"version":true,"op":"health"}'))["status"], "DENIED")


if __name__ == "__main__":
    unittest.main()
