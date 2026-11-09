"""Transport-drop simulation; NOT a detached-process acceptance test."""
import unittest
from sora_commander_next.broker_contract import BoundRequest
from sora_commander_next.broker_runtime import BrokerRuntime
from sora_commander_next.broker_dispatch import BrokerDispatcher


class Guard:
    def verify(self):
        return True


class FakeManager:
    def __init__(self):
        self.started = 0
        self.closed = False
        self.status = "RUNNING"
        self.request_id = None

    def start(self, command):
        self.started += 1
        self.request_id = command.request_id
        # The current BrokerRuntime requires exact request/session correlation.
        # A mock without request_id must be rejected as UNKNOWN, not launched.
        return {"status": "RUNNING", "request_id": command.request_id,
                "session_id": "a" * 48}

    def read(self, session):
        return {"status": self.status, "request_id": self.request_id,
                "session_id": session,
                "stdout": "done" if self.status == "COMPLETED" else ""}

    def stop(self, session):
        return {"status": "CANCELLING"}

    def close(self):
        self.closed = True


class TransportLifetimeTests(unittest.TestCase):
    def test_disconnect_does_not_close_runtime_or_duplicate_launch(self):
        guard = Guard()
        manager = FakeManager()
        req = BoundRequest("f" * 32, "C:/fixed.exe", ("-File", "fixed.ps1"),
                           "D:/repo", "b" * 64, "owner", 1000.0, 300.0, 8192)
        runtime = BrokerRuntime(expected_executable=req.executable,
                                expected_cwd=req.cwd, owner_binding="owner",
                                owner_guard=guard, clock=lambda: 100,
                                manager=manager)
        self.assertEqual(runtime.start_approved(
            req, approved_fingerprint=req.fingerprint(),
            current_task_sha256=req.task_sha256)["status"], "RUNNING")
        first = BrokerDispatcher(owner_guard=guard, lookup=runtime.query,
                                 cancel=runtime.cancel)
        self.assertIsNotNone(first.handle(b'{"version":1,"op":"health"}'))
        del first  # Simulate transport lifetime ending; runtime remains owned elsewhere.
        manager.status = "COMPLETED"
        second = BrokerDispatcher(owner_guard=guard, lookup=runtime.query,
                                  cancel=runtime.cancel)
        self.assertEqual(runtime.query(req.request_id)["stdout"], "done")
        self.assertEqual(manager.started, 1)
        self.assertFalse(manager.closed)
        self.assertIn(b'"status":"OK"', second.handle(
            ('{"version":1,"op":"query","request_id":"' + req.request_id + '"}').encode()))
        runtime.close()
        self.assertTrue(manager.closed)


if __name__ == "__main__":
    unittest.main()
