import json
import unittest
from sora_commander_next.broker_transport_gate import BrokerTransportGate


class Dispatcher:
    def __init__(self):
        self.calls = []
    def handle(self, raw):
        self.calls.append(raw)
        return b'{"version":1,"status":"OK","request_id":null}'


class TransportGateTests(unittest.TestCase):
    def setUp(self):
        self.dispatcher = Dispatcher()
        self.frame = b'{"version":1,"op":"health"}'

    def send(self, verifier, frame=None):
        gate = BrokerTransportGate(verify_client=verifier, dispatcher=self.dispatcher)
        return json.loads(gate.handle_verified_connection(self.frame if frame is None else frame))

    def test_verified_client(self):
        self.assertEqual(self.send(lambda: True)["status"], "OK")
        self.assertEqual(self.dispatcher.calls, [self.frame])

    def test_unverified_client(self):
        self.assertEqual(self.send(lambda: False)["status"], "DENIED")
        self.assertEqual(self.dispatcher.calls, [])

    def test_truthy_identity_not_enough(self):
        self.assertEqual(self.send(lambda: 1)["status"], "DENIED")
        self.assertEqual(self.dispatcher.calls, [])

    def test_missing_token(self):
        def missing():
            raise PermissionError("no impersonation token")
        self.assertEqual(self.send(missing)["status"], "DENIED")
        self.assertEqual(self.dispatcher.calls, [])

    def test_invalid_frame_before_verifier(self):
        calls = []
        self.assertEqual(self.send(lambda: calls.append(1) or True, b"x"*8193)["status"], "DENIED")
        self.assertEqual(calls, [])
        self.assertEqual(self.dispatcher.calls, [])

    def test_dispatcher_failure_is_error(self):
        class Broken:
            def handle(self, raw):
                raise RuntimeError("broken")
        gate = BrokerTransportGate(verify_client=lambda: True, dispatcher=Broken())
        self.assertEqual(json.loads(gate.handle_verified_connection(self.frame))["status"], "ERROR")

    def test_constructor_requires_verifier(self):
        with self.assertRaises(TypeError):
            BrokerTransportGate(verify_client=None, dispatcher=self.dispatcher)


if __name__ == "__main__":
    unittest.main()
