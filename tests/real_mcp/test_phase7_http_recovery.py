"""Real MCP 1.30.0 HTTP reconnect after disposable Host crash.

Run explicitly with repository .venv; not included in stdlib core discovery.
No production configuration, persistent secret, service or external transport.
"""
from datetime import timedelta
import importlib.metadata
import os
from pathlib import Path
import secrets
import signal
import socket
import sys
from threading import Thread
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.host_supervisor import HostSupervisor

TOOLS = {"run_command", "read_process_output", "stop_process", "ping", "status"}
# Local-only actions must never become remote tools. The exact list is in test_mcp_ai_tool_catalog.
NEVER_REMOTE = {"approve_local", "reject_local", "add_grant_local", "replace_text"}


def require_sdk():
    if Path(sys.executable).resolve() != (ROOT / ".venv/Scripts/python.exe").resolve():
        raise RuntimeError("Use repository .venv/Scripts/python.exe")
    if importlib.metadata.version("mcp") != "1.30.0":
        raise RuntimeError("Expected mcp 1.30.0")


def reserve_test_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_for_authenticated_server(url, secret, watcher, generation):
    import anyio
    import httpx
    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state = watcher.snapshot()
            if state["generation"] >= generation and state["host_pid"] is not None:
                try:
                    denied = await client.get(url)
                    if denied.status_code == 401:
                        unauthorized = await client.get(url, headers={"Authorization": "Bearer incorrect"})
                        assert unauthorized.status_code == 401
                        return state["host_pid"]
                except httpx.TransportError:
                    pass
            await anyio.sleep(.04)
    raise AssertionError(f"HTTP generation {generation} not ready: {watcher.snapshot()}")


async def mcp_session(url, secret):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    token_header = {"Authorization": "Bearer " + secret}
    def make_client(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                                 trust_env=False, follow_redirects=True)

    async with streamablehttp_client(url, headers=token_header, timeout=5,
            sse_read_timeout=5, httpx_client_factory=make_client) as (read, write, get_id):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session:
            assert (await session.initialize()).protocolVersion
            names = {x.name for x in (await session.list_tools()).tools}
            assert TOOLS <= names and not NEVER_REMOTE & names
            await session.send_ping()
            ping = await session.call_tool("ping", {})
            assert ping.structuredContent["status"] == "OK"
            status = await session.call_tool("status", {})
            assert status.structuredContent["remote_ready"] is False
            assert status.structuredContent["approval"] == "LOCAL_DEMO_ONLY"
            assert status.structuredContent["sessions"] == "IN_MEMORY"
            assert status.structuredContent["process_descendants"] == "WINDOWS_JOB_OBJECT"

            denied = await session.call_tool("run_command", {
                "executable": sys.executable, "argv": ["--version"], "cwd": str(ROOT)
            })
            assert denied.structuredContent["status"] == "DENY"
            malformed = await session.call_tool("run_command", {"argv": "bad"})
            assert malformed.isError
            after = await session.call_tool("ping", {})
            assert after.structuredContent["status"] == "OK"
            identifier = get_id()
            assert isinstance(identifier, str) and identifier
            assert secret not in str(denied) + str(status) + str(malformed)
            return identifier


class Phase7HTTPRecoveryTests(unittest.TestCase):
    def test_crash_and_authenticated_real_mcp_reconnect_without_command_replay(self):
        require_sdk()
        import anyio
        import httpx
        secret = secrets.token_hex(32)
        port = reserve_test_port()
        url = f"http://127.0.0.1:{port}/mcp"
        program = ROOT / "tests/real_mcp/phase7_http_host.py"
        watcher = HostSupervisor(
            [sys.executable, "-X", "utf8", str(program), str(port)], str(ROOT),
            max_restarts=1, poll_seconds=.02, backoff_seconds=.08,
            startup_token=secret.encode("ascii")
        )
        runner = Thread(target=watcher.run, daemon=True)
        runner.start()
        initial_pid = None
        second_pid = None
        try:
            initial_pid = anyio.run(wait_for_authenticated_server, url, secret, watcher, 1)
            old_id = anyio.run(mcp_session, url, secret)
            self.assertEqual(watcher.snapshot()["generation"], 1)

            # Force-kill the one disposable HTTP Host only, NOT the supervisor.
            os.kill(initial_pid, signal.SIGTERM)
            second_pid = anyio.run(wait_for_authenticated_server, url, secret, watcher, 2)
            self.assertNotEqual(initial_pid, second_pid)

            new_id = anyio.run(mcp_session, url, secret)
            self.assertNotEqual(old_id, new_id)

            # A session ID from the first Host is not a valid session after a
            # full Host restart (even when authenticated with the same token).
            async def reject_stale_session():
                async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                    response = await client.get(url, headers={
                        "Authorization": "Bearer " + secret,
                        "Mcp-Session-Id": old_id,
                        "Accept": "text/event-stream"
                    })
                    self.assertEqual(response.status_code, 404)
            anyio.run(reject_stale_session)
            snapshot = watcher.snapshot()
            self.assertEqual(snapshot["generation"], 2)
            self.assertEqual(snapshot["restarts"], 1)
            self.assertFalse(snapshot["replay"])
            self.assertFalse(snapshot["remote_ready"])
            self.assertEqual(snapshot["state"], "RUNNING")
        finally:
            watcher.stop()
            runner.join(timeout=5)
            self.assertFalse(runner.is_alive(), "watchdog must stop its owned Host")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(.6)
                self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0,
                    "disposable loopback MCP listener survived cleanup")
        print({"sdk": "1.30.0", "transport": "LOOPBACK_HTTP", "crash_restarted": True,
               "new_pid": initial_pid != second_pid, "auth": "PASS",
               "old_session_rejected": True, "command_replay": False,
               "listener_closed": True}, flush=True)


if __name__ == "__main__":
    unittest.main()
