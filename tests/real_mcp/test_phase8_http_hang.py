"""Windows real MCP SDK Phase 8: hung-but-live HTTP host recovery.

Run explicitly with .venv Python 3.13 / MCP SDK 1.30.0.
Temporary 127.0.0.1 Host, no Task, service, external tunnel or grants.
"""
import os
from pathlib import Path
import secrets
import socket
import sys
from threading import Thread
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sora_commander_next.local_health import LocalHealthProbe
from sora_commander_next.host_supervisor import HostSupervisor
from test_phase7_http_recovery import (require_sdk, reserve_test_port,
                                      wait_for_authenticated_server, mcp_session)


class Phase8HTTPHangTests(unittest.TestCase):
    def make(self, port, token, *, marker=None, health_token=None):
        program = ROOT / "tests/real_mcp/phase7_http_host.py"
        args = [sys.executable, "-X", "utf8", str(program), str(port)]
        if marker is not None:
            args.append(str(marker))
        probe = LocalHealthProbe(port, health_token or token, timeout=.20)
        return HostSupervisor(args, str(ROOT), max_restarts=1,
            startup_token=token, health_probe=probe, health_interval=.05,
            startup_grace=7, health_failures=3, backoff_seconds=.10,
            poll_seconds=.02)

    def run_watcher(self, watcher):
        thread = Thread(target=watcher.run, daemon=True)
        thread.start()
        return thread

    def cleanup(self, watcher, thread, port):
        watcher.stop()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "watchdog failed to shut down")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(.5)
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)),0,
                                "test HTTP listener remained after watchdog shutdown")

    def until(self, watcher, condition, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            status = watcher.snapshot()
            if condition(status):
                return status
            time.sleep(.025)
        self.fail(f"watch condition not reached: {watcher.snapshot()}")

    def test_real_hung_host_recovered_with_authenticated_mcp_and_no_replay(self):
        require_sdk()
        import anyio
        import httpx
        token = secrets.token_hex(32).encode("ascii")
        port = reserve_test_port()
        url = f"http://127.0.0.1:{port}/mcp"
        with tempfile.TemporaryDirectory(prefix="sora-phase8-") as directory:
            marker = Path(directory) / "block-event-loop"
            watcher = self.make(port, token, marker=marker)
            thread = self.run_watcher(watcher)
            try:
                first = self.until(watcher,lambda s:s["state"] == "RUNNING" and s["host_pid"])
                self.assertEqual(watcher.snapshot()["generation"], 1)
                old_id = anyio.run(mcp_session, url, token.decode())

                # The fixture only stalls its HTTP event loop on this local
                # marker: process stays alive, but ALL MCP traffic hangs.
                marker.write_text("simulate hang", encoding="utf-8")
                restarted = self.until(watcher,
                    lambda s:s["generation"] == 2 and s["last_restart_reason"] == "HEALTH_FAILURE",
                    seconds=8)
                marker.unlink()
                self.assertNotEqual(first["host_pid"], restarted["host_pid"])
                self.assertEqual(restarted["restarts"], 1)
                self.assertFalse(restarted["replay"])
                self.until(watcher,lambda s:s["state"] == "RUNNING" and s["generation"] == 2)
                second_id = anyio.run(mcp_session, url, token.decode())
                self.assertNotEqual(old_id, second_id)
                async def test_old_session():
                    async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                        response = await client.get(url, headers={
                            "Authorization": "Bearer "+token.decode(),
                            "Mcp-Session-Id": old_id,
                            "Accept": "text/event-stream"})
                        self.assertEqual(response.status_code,404)
                anyio.run(test_old_session)
                self.assertEqual(watcher.snapshot()["restarts"],1)
                print({"phase":8,"sdk":"1.30.0","hang_detected":True,
                    "different_host_pid":True,"mcp_reconnected":True,
                    "old_session_rejected":True,"automatic_replay":False},flush=True)
            finally:
                if marker.exists():
                    marker.unlink()
                self.cleanup(watcher,thread,port)

    def test_bad_watchdog_credential_stops_without_restarting_host(self):
        require_sdk()
        token=secrets.token_hex(32).encode()
        other=secrets.token_hex(32).encode()
        port=reserve_test_port()
        watcher=self.make(port,token,health_token=other)
        thread=self.run_watcher(watcher)
        try:
            status=self.until(watcher,lambda s:s["state"] == "AUTH_FAILED")
            self.assertEqual(status["generation"],1)
            self.assertEqual(status["restarts"],0)
            self.assertIsNone(status["host_pid"])
            print({"phase":8,"wrong_health_token":"AUTH_FAILED",
                   "restarts":0},flush=True)
        finally:
            self.cleanup(watcher,thread,port)


if __name__ == "__main__":
    unittest.main()
