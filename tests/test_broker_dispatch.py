import json
import unittest
from sora_commander_next.broker_dispatch import BrokerDispatcher


class Guard:
    allowed = True
    def verify(self):
        if not self.allowed:
            raise PermissionError("locked")


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.guard = Guard()
        self.calls = []
        self.rid = "a" * 32
        self.dispatcher = BrokerDispatcher(
            owner_guard=self.guard,
            lookup=lambda rid: self.calls.append(("lookup", rid)) or {"status": "RUNNING"},
            cancel=lambda rid: self.calls.append(("cancel", rid)) or True,
        )

    def send(self, op, **kwargs):
        return json.loads(self.dispatcher.handle(
            json.dumps({"version": 1, "op": op, **kwargs}).encode("utf-8")))

    def test_health(self):
        self.assertEqual(self.send("health")["status"], "OK")

    def test_query(self):
        self.assertEqual(self.send("query", request_id=self.rid)["status"], "OK")
        self.assertEqual(self.calls, [("lookup", self.rid)])

    def test_cancel(self):
        self.assertEqual(self.send("cancel", request_id=self.rid)["status"], "OK")
        self.assertEqual(self.calls, [("cancel", self.rid)])

    def test_locked_denies_without_backend_call(self):
        self.guard.allowed = False
        self.assertEqual(self.send("cancel", request_id=self.rid)["status"], "DENIED")
        self.assertEqual(self.calls, [])

    def test_unknown_is_not_success(self):
        d = BrokerDispatcher(owner_guard=self.guard, lookup=lambda _: None, cancel=lambda _: False)
        self.assertEqual(json.loads(d.handle(json.dumps(
            {"version": 1, "op": "query", "request_id": self.rid}).encode()))["status"], "UNKNOWN")

    def test_unknown_status_dict_is_not_misreported_as_ok(self):
        for state in ("UNKNOWN", "OUTCOME_UNKNOWN", "INTERRUPTED_UNKNOWN",
                      "DENIED", "ERROR", "UNRECOGNIZED"):
            with self.subTest(state=state):
                d = BrokerDispatcher(
                    owner_guard=self.guard, lookup=lambda _: {"status": state},
                    cancel=lambda _: False)
                raw = json.dumps({"version": 1, "op": "query",
                                  "request_id": self.rid}).encode()
                self.assertEqual(json.loads(d.handle(raw))["status"], "UNKNOWN")

    def test_verified_terminal_status_remains_known_without_claiming_success(self):
        for state in ("RUNNING", "COMPLETED", "FAILED", "TIMEOUT", "CANCELLED"):
            with self.subTest(state=state):
                d = BrokerDispatcher(
                    owner_guard=self.guard, lookup=lambda _: {"status": state},
                    cancel=lambda _: False)
                raw = json.dumps({"version": 1, "op": "query",
                                  "request_id": self.rid}).encode()
                self.assertEqual(json.loads(d.handle(raw))["status"], "OK")

    def test_bad_frame_does_not_call_backend(self):
        self.assertEqual(json.loads(self.dispatcher.handle(b'{"op":"run_command"}'))["status"], "DENIED")
        self.assertEqual(self.calls, [])

    def test_backend_error_is_unknown(self):
        def broken(_):
            raise RuntimeError("backend unavailable")
        d = BrokerDispatcher(owner_guard=self.guard, lookup=broken, cancel=broken)
        self.assertEqual(json.loads(d.handle(json.dumps(
            {"version": 1, "op": "cancel", "request_id": self.rid}).encode()))["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
