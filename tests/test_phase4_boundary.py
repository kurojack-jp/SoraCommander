"""Stdlib ASGI authentication boundary checks; not SDK/HTTP acceptance."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.http_local import _BearerGate, create_loopback_app

# Synthetic test value only; never used by the real HTTP server.
TOKEN = "0123456789abcdef" + "fedcba9876543210" + "13579bdf02468ace" + "eca86420fdb97531"


class Phase4BoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_invalid_configuration_fails_before_sdk_import_or_bind(self):
        for host in ("0.0.0.0", "localhost", "::1", "127.0.0.2", None, 123):
            with self.subTest(host=host), self.assertRaises(ValueError):
                create_loopback_app(TOKEN, host=host)
        for token in (None, "", b"a" * 64, "a" * 64, "0123456789abcdef" * 4,
                      "x" * 64, "abc", "a" * 63 + "b", TOKEN + "\n"):
            with self.subTest(token_type=type(token)), self.assertRaises(ValueError):
                create_loopback_app(token)

    async def test_every_method_and_path_rejects_missing_malformed_duplicate_auth(self):
        calls = []

        async def app(scope, receive, send):
            calls.append(scope)

        gate = _BearerGate(app, TOKEN)
        valid = (b"authorization", b"Bearer " + TOKEN.encode())
        invalid = ([], [(b"authorization", b"Bearer wrong")],
                   [(b"authorization", b"bearer " + TOKEN.encode())],
                   [(b"authorization", b"Bearer  " + TOKEN.encode())],
                   [(b"authorization", valid[1] + b" ")],
                   [(b"authorization", valid[1] + b", " + valid[1])],
                   [valid, valid], [valid, (b"Authorization", b"Bearer wrong")])
        for method in ("GET", "POST", "DELETE", "PUT", "PATCH", "HEAD", "OPTIONS"):
            for path in ("/mcp", "/mcp/", "/mcp/nested", "/other"):
                for headers in invalid:
                    sent = []

                    async def send(message):
                        sent.append(message)

                    async def receive():
                        self.fail("Unauthorized body must never be read")

                    await gate({"type": "http", "method": method, "path": path,
                                "headers": headers}, receive, send)
                    self.assertEqual(sent[0]["status"], 401)
                    self.assertEqual(sent[1]["body"], b"")
        self.assertEqual(calls, [])

    async def test_valid_request_and_lifespan_pass_through_websocket_denied(self):
        calls, sent = [], []

        async def app(scope, receive, send):
            calls.append(scope["type"])

        async def send(message):
            sent.append(message)

        gate = _BearerGate(app, TOKEN)
        await gate({"type": "http", "headers": [
            (b"Authorization", b"Bearer " + TOKEN.encode())]}, None, send)
        await gate({"type": "lifespan"}, None, send)
        await gate({"type": "websocket"}, None, send)
        self.assertEqual(calls, ["http", "lifespan"])
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])


if __name__ == "__main__":
    unittest.main()
