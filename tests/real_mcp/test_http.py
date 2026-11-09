"""Opt-in real mcp 1.30.0 HTTP test, with a disposable deny-only child server.

Run explicitly with repo .venv Python. Never collected by stdlib core discovery.
Token travels over an anonymous stdin pipe, never argv, environment or a file.
"""

from datetime import timedelta
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
TOOLS = {"run_command", "read_process_output", "stop_process", "ping", "status", "list_folder", "read_text_file"}
# Local-only actions must never become remote tools. The exact list is in test_mcp_ai_tool_catalog.
NEVER_REMOTE = {"approve_local", "reject_local", "add_grant_local", "replace_text"}


def require_sdk():
    if Path(sys.executable).resolve() != (ROOT / ".venv/Scripts/python.exe").resolve():
        raise RuntimeError("Use repository .venv/Scripts/python.exe")
    if importlib.metadata.version("mcp") != "1.30.0":
        raise RuntimeError("This milestone requires installed mcp 1.30.0")


def serve_test_child():
    """Only this explicit test entrypoint binds, always to ephemeral loopback."""
    import uvicorn
    from sora_commander_next.http_local import create_loopback_app

    # Bounds orphan lifetime even if the parent or SDK cleanup hangs.
    timer = threading.Timer(35, lambda: os._exit(124))
    timer.daemon = True
    timer.start()
    try:
        token = sys.stdin.readline(128).removesuffix("\n")
        app = create_loopback_app(token, host="127.0.0.1")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(32)
            print(json.dumps({"port": listener.getsockname()[1], "pid": os.getpid()}), flush=True)
            config = uvicorn.Config(app, host="127.0.0.1", port=0, access_log=False,
                                    log_level="critical", proxy_headers=False,
                                    lifespan="on", timeout_graceful_shutdown=2,
                                    ws="none")
            server = uvicorn.Server(config)

            def stop_on_parent_eof():
                sys.stdin.read()
                server.should_exit = True

            threading.Thread(target=stop_on_parent_eof, daemon=True).start()
            server.run(sockets=[listener])
    finally:
        timer.cancel()


async def exercise(url, token, process):
    import anyio
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    check = unittest.TestCase()
    headers = {"Authorization": "Bearer " + token}

    async def tool(session, name, args=None):
        result = await session.call_tool(name, args or {})
        check.assertFalse(result.isError)
        check.assertIsInstance(result.structuredContent, dict)
        check.assertEqual(json.loads(result.content[0].text), result.structuredContent)
        check.assertTrue(token not in str(result), "Token appeared in tool response")
        return result.structuredContent

    async def healthy(session):
        await session.send_ping()
        check.assertEqual((await tool(session, "ping"))["status"], "OK")
        status = await tool(session, "status")
        check.assertFalse(status["remote_ready"])
        check.assertEqual(status["sessions"], "IN_MEMORY")
        check.assertEqual(status["approval"], "LOCAL_DEMO_ONLY")

    async with httpx.AsyncClient(trust_env=False, timeout=2) as http:
        deadline = time.monotonic() + 8
        while True:
            check.assertIsNone(process.poll(), "HTTP child exited before readiness")
            try:
                response = await http.post(url)
                check.assertEqual(response.status_code, 401)
                break
            except httpx.TransportError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Loopback readiness deadline") from None
                await anyio.sleep(.05)

        session_ids = []
        for connection in range(2):
            # Actual SDK factory: disable env proxies without substituting transport.
            def http_factory(headers=None, timeout=None, auth=None):
                return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                                         trust_env=False, follow_redirects=True)

            async with streamablehttp_client(url, headers=headers, timeout=5,
                    sse_read_timeout=5, httpx_client_factory=http_factory) as (read, write, get_id):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session:
                    check.assertTrue((await session.initialize()).protocolVersion)
                    names = {t.name for t in (await session.list_tools()).tools}
                    check.assertLessEqual(TOOLS, names)
                    check.assertFalse(NEVER_REMOTE & names)
                    session_id = get_id()
                    check.assertIsInstance(session_id, str)
                    check.assertNotIn(session_id, session_ids)
                    session_ids.append(session_id)
                    await healthy(session)
                    if connection == 0:
                        # Bad auth against a LIVE session must not destroy it.
                        for method in ("POST", "GET", "DELETE", "OPTIONS", "PUT", "PATCH", "HEAD"):
                            for auth_headers in ([], [("Authorization", "Bearer wrong")],
                                    [("Authorization", headers["Authorization"])] * 2,
                                    [("Authorization", "Bearer  " + token)],
                                    [("Authorization", "Basic " + token)]):
                                response = await http.request(method, url, headers=[
                                    *auth_headers, ("Mcp-Session-Id", session_id)])
                                check.assertEqual(response.status_code, 401)
                                check.assertTrue(token not in response.text, "Token appeared in HTTP response")
                        await healthy(session)
                        # Keep SDK DNS rebinding protection enabled.
                        for override, value in (("Host", "evil.invalid"), ("Origin", "https://evil.invalid")):
                            response = await http.post(url, headers=headers | {override: value}, json={})
                            check.assertEqual(response.status_code, 421 if override == "Host" else 403)
                        # Wire parse error and thrown ToolError, then live health.
                        response = await http.post(url, headers=headers | {
                            "Mcp-Session-Id": session_id,
                            "Content-Type": "application/json",
                            "Accept": "application/json, text/event-stream"}, content=b"{broken")
                        check.assertEqual(response.status_code, 400)
                        bad = await session.call_tool("run_command", {"argv": "malformed"})
                        check.assertTrue(bad.isError)
                        forbidden = await session.call_tool("approve_local", {"pending_id": "synthetic"})
                        check.assertTrue(forbidden.isError)
                        await healthy(session)
                    denied = await tool(session, "run_command", {
                        "executable": sys.executable, "argv": ["--version"], "cwd": str(ROOT)})
                    check.assertEqual(denied["status"], "DENY")
                    for name in ("read_process_output", "stop_process"):
                        result = await tool(session, name, {"session_id": "f" * 48})
                        check.assertEqual(result["status"], "ERROR")
                        check.assertEqual(result["error"], "UnknownSession")
                    await healthy(session)
                    check.assertIsNone(process.poll())
            # SDK DELETE terminated the session; unauthenticated DELETE did not.
            response = await http.get(url, headers=headers | {
                "Mcp-Session-Id": session_id, "Accept": "text/event-stream"})
            check.assertEqual(response.status_code, 404)
        check.assertIsNone(process.poll(), "Both sessions must use the same live child")
    return {"sdk": "1.30.0", "transport": "LOOPBACK_HTTP", "sessions": 2,
            "same_child": True, "auth": "PASS", "denial_and_error_survival": "PASS"}


class RealHTTPTests(unittest.TestCase):
    def test_two_real_sessions_auth_and_error_survival(self):
        require_sdk()
        result = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                                 "--scenario"], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip(), flush=True)

    def run_scenario(self):
        require_sdk()
        import anyio
        token = secrets.token_hex(32)
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
               "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--server"],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8")

        def stop_owned():
            # Windows venv python.exe may be a launcher whose PID differs from
            # the interpreter. EOF stops the actual server, not just its launcher.
            if not process.stdin.closed:
                process.stdin.close()

        watchdog = threading.Timer(30, stop_owned)
        watchdog.start()
        port = None
        try:
            process.stdin.write(token + "\n")
            process.stdin.flush()
            announcement = process.stdout.readline()
            if not announcement:
                raise RuntimeError("Loopback child failed before readiness")
            info = json.loads(announcement)
            self.assertTrue(type(info["pid"]) is int and info["pid"] > 0)
            port = info["port"]
            self.assertTrue(type(port) is int and 0 < port < 65536)

            async def bounded():
                with anyio.fail_after(20):
                    return await exercise(f"http://127.0.0.1:{port}/mcp", token, process)

            result = anyio.run(bounded)
        finally:
            stop_owned()
            # The child has its own 35s hard exit even if graceful shutdown hangs.
            # Waiting for the launcher also waits for its interpreter on Windows.
            try:
                process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise RuntimeError("Server failed its independent hard deadline") from None
            watchdog.cancel()
            watchdog.join()
            stdout, stderr = process.stdout.read(), process.stderr.read()
            process.stdout.close()
            process.stderr.close()
            self.assertTrue(token not in stdout + stderr, "Token appeared in server output")
            self.assertIsNotNone(process.returncode)
            if port is not None:
                with socket.socket() as probe:
                    probe.settimeout(.5)
                    self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0,
                                        "Disposable listener survived child cleanup")
        self.assertEqual(process.returncode, 0, "HTTP server did not shut down normally")
        print(json.dumps(result | {"server_exit": 0, "child_reaped": True,
                                  "listener_closed": True}), flush=True)


if __name__ == "__main__":
    require_sdk()
    if sys.argv[1:] == ["--server"]:
        serve_test_child()
    elif sys.argv[1:] == ["--scenario"]:
        RealHTTPTests().run_scenario()
    else:
        unittest.main()
