"""Phase 8 local-only authenticated health boundary; stdlib, no MCP SDK."""
from pathlib import Path
import sys
from threading import Thread
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.http_local import _BearerGate
from sora_commander_next.local_health import LocalHealthProbe
from sora_commander_next.host_supervisor import HostSupervisor

TOKEN = "0123456789abcdef" + "fedcba9876543210" + "13579bdf02468ace" + "eca86420fdb97531"


class Phase8HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_bearer_health_204_no_body_and_no_other_routes(self):
        routed, sent = [], []

        async def delegate(scope, receive, send):
            routed.append(scope["path"])

        async def send(message):
            sent.append(message)

        health_count = []
        def health():
            health_count.append(1)
            return {"status": "OK", "scaffold": True, "remote_ready": False}
        gate = _BearerGate(delegate, TOKEN, health)
        basic = [(b"authorization", b"Bearer " + TOKEN.encode()),
                 (b"host", b"127.0.0.1:12345")]
        async def invoke(method="GET", path="/__sora_health", headers=basic):
            sent.clear()
            await gate({"type": "http", "method": method, "path": path, "headers": headers},
                       None, send)
            return list(sent)
        messages = await invoke()
        self.assertEqual(messages[0]["status"], 204)
        self.assertEqual(messages[1]["body"], b"")
        self.assertEqual(messages[0]["headers"][0], (b"cache-control", b"no-store"))
        self.assertEqual(len(health_count), 1)
        for args in [
            {"method": "POST"}, {"method": "HEAD"},
            {"headers": basic + [(b"origin", b"http://127.0.0.1:12345")]},
            {"headers": basic[:-1] + [(b"host", b"evil.invalid")]},
            {"headers": basic[:-1] + [(b"host", b"127.0.0.1:12345"), (b"host", b"127.0.0.1:12345")]},
        ]:
            self.assertEqual((await invoke(**args))[0]["status"], 503)
        self.assertEqual((await invoke(headers=basic[1:]))[0]["status"], 401)
        self.assertEqual((await invoke(headers=basic[1:] + [(b"authorization", b"Bearer bad")]))[0]["status"], 401)
        self.assertEqual(len(health_count), 1)
        self.assertEqual(routed, [])

        await invoke(path="/mcp")
        self.assertEqual(routed, ["/mcp"])

    async def test_health_exception_not_exposed_or_propagated(self):
        async def delegate(scope, receive, send):
            self.fail("failure must not reach SDK")
        def fail():
            raise RuntimeError("SECRET")
        gate = _BearerGate(delegate, TOKEN, fail)
        messages = []
        async def send(m):
            messages.append(m)
        await gate({"type": "http", "method": "GET", "path": "/__sora_health",
                    "headers": [(b"authorization", b"Bearer " + TOKEN.encode()),
                                (b"host", b"127.0.0.1:55555")]}, None, send)
        self.assertEqual(messages[0]["status"], 503)
        self.assertNotIn("SECRET", str(messages))

    def test_invalid_probe_config_fail_closed_without_network(self):
        for port,token in ((0,TOKEN.encode()), (65536,TOKEN.encode()),
                           (True,TOKEN.encode()),(5678,b"wrong"),
                           (5678,TOKEN), (5678,b"a"*64)):
            with self.subTest(port=port), self.assertRaises(ValueError):
                LocalHealthProbe(port,token)
        probe = LocalHealthProbe(65534, TOKEN.encode(),timeout=.05)
        self.assertEqual(probe(), "FAIL")

    def test_temporary_health_probe_failure_does_not_restart_host(self):
        calls = []
        def transient():
            calls.append(1)
            return "FAIL" if len(calls) < 3 else "OK"
        watcher = HostSupervisor([sys.executable, "-I", "-S", "-c",
                                  "import time;time.sleep(.5)"], str(ROOT),
                                  max_restarts=0, health_probe=transient,
                                  health_interval=.05, startup_grace=.6,
                                  health_failures=3)
        thread = Thread(target=watcher.run, daemon=True)
        thread.start()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        status = watcher.snapshot()
        self.assertEqual(status["state"], "CLEAN_EXIT")
        self.assertEqual(status["generation"], 1)
        self.assertEqual(status["restarts"], 0)
        self.assertGreaterEqual(len(calls), 3)

    def test_invalid_watchdog_health_budgets(self):
        for kwargs in ({"health_probe": "not callable"},
                       {"health_interval":0}, {"health_interval":4},
                       {"startup_grace": 0}, {"health_failures":0},
                       {"health_failures":True}, {"health_failures": 50}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                HostSupervisor([sys.executable], str(ROOT), **kwargs)


if __name__ == "__main__":
    unittest.main()
