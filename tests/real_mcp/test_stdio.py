"""Opt-in real SDK stdio tests. Run this file explicitly with project .venv Python.

SDK process creation is observed, never replaced with a fake. Each scenario has
an independent 30s watchdog and the parent has a 40s subprocess deadline.
"""

from datetime import timedelta
from contextlib import asynccontextmanager
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8",
       "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
TOOLS = {"run_command", "read_process_output", "stop_process", "ping", "status", "list_folder", "read_text_file"}
# Local-only actions must never become remote tools. The exact list is in test_mcp_ai_tool_catalog.
NEVER_REMOTE = {"approve_local", "reject_local", "add_grant_local", "replace_text"}


def require_venv_sdk():
    if Path(sys.executable).resolve() != (ROOT / ".venv/Scripts/python.exe").resolve():
        raise RuntimeError("Use this repository's .venv/Scripts/python.exe")
    try:
        return importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        raise RuntimeError("MCP SDK missing from project .venv; no installation attempted") from None


async def scenario(mode, processes):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    import mcp.client.stdio as stdio

    memory_transport = mode.startswith("memory-")
    mode = mode.removeprefix("memory-")

    create_process = stdio._create_platform_compatible_process

    async def observe_process(*args, **kwargs):
        process = await create_process(*args, **kwargs)
        processes.append(process)
        return process

    server_args = ([str(ROOT / "tests/real_mcp/fixture_server.py")] if mode == "fixture"
                   else ["-m", "sora_commander_next.stdio_main"])
    params = StdioServerParameters(command=sys.executable, args=server_args,
                                   cwd=str(ROOT), env={key: ENV[key] for key in
                                   ("PYTHONPATH", "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONDONTWRITEBYTECODE")})
    assertions = unittest.TestCase()

    @asynccontextmanager
    async def transport():
        if not memory_transport:
            async with stdio.stdio_client(params) as streams:
                yield streams
            return
        # Separate real SDK coverage when Windows denies async stdio pipe creation.
        # This is SDK memory transport, never counted as stdio acceptance.
        from mcp.shared.memory import create_client_server_memory_streams
        from sora_commander_next.host import Host
        from sora_commander_next.mcp_adapter import build_server
        from fixture_server import build_host
        host = build_host() if mode == "fixture" else Host()
        server = build_server(host)._mcp_server
        try:
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                async with anyio.create_task_group() as group:
                    group.start_soon(server.run, *server_streams, server.create_initialization_options())
                    try:
                        yield client_streams
                    finally:
                        group.cancel_scope.cancel()
        finally:
            host.jobs.close()

    async def tool(session, name, arguments=None):
        result = await session.call_tool(name, arguments or {})
        assertions.assertFalse(result.isError, result)
        assertions.assertIsInstance(result.structuredContent, dict)
        assertions.assertEqual(json.loads(result.content[0].text), result.structuredContent)
        return result.structuredContent

    async def healthy(session):
        await session.send_ping()  # Protocol ping, separately from the ping tool.
        assertions.assertEqual((await tool(session, "ping"))["status"], "OK")
        status = await tool(session, "status")
        assertions.assertFalse(status["remote_ready"])
        assertions.assertEqual(status["approval"], "LOCAL_DEMO_ONLY")
        assertions.assertNotIn("PHASE3_PRIVATE_MARKER", json.dumps(status))

    with patch.object(stdio, "_create_platform_compatible_process", observe_process):
        with anyio.fail_after(20):
            async with transport() as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session:
                    initialized = await session.initialize()
                    assertions.assertTrue(initialized.protocolVersion)
                    listed = await session.list_tools()
                    names = {entry.name for entry in listed.tools}
                    assertions.assertLessEqual(TOOLS, names)
                    assertions.assertFalse(NEVER_REMOTE & names)
                    schemas = {entry.name: entry for entry in listed.tools}
                    schema = schemas["run_command"].inputSchema
                    assertions.assertEqual(set(schema["required"]), {"executable", "argv", "cwd"})
                    assertions.assertEqual(schema["properties"]["argv"]["items"]["type"], "string")
                    assertions.assertEqual(schema["properties"]["background"]["type"], "boolean")
                    assertions.assertIsNotNone(schemas["run_command"].outputSchema)
                    await healthy(session)
                    valid = {"executable": sys.executable, "argv": ["--version"],
                             "cwd": str(ROOT / "tests" if mode == "fixture" else ROOT)}
                    # Check missing/unknown fields and types before SDK coercion.
                    malformed = [{}, valid | {"timeout": True}, valid | {"background": "true"},
                                 valid | {"output_limit": 128.0}, valid | {"argv": "--version"},
                                 valid | {"argv": [1]}, valid | {"cwd": None},
                                 valid | {"env": {"PHASE3_PRIVATE_MARKER": "synthetic"}}]
                    for arguments in malformed:
                        result = await session.call_tool("run_command", arguments)
                        assertions.assertTrue(result.isError, result)
                        assertions.assertNotIn("PHASE3_PRIVATE_MARKER", str(result))
                        await healthy(session)
                    remote_approval = await session.call_tool("approve_local", {"pending_id": "PHASE3_PRIVATE_MARKER"})
                    assertions.assertTrue(remote_approval.isError)
                    assertions.assertNotIn("PHASE3_PRIVATE_MARKER", str(remote_approval))
                    await healthy(session)
                    denied = await tool(session, "run_command", valid | {"cwd": str(ROOT / "src")})
                    assertions.assertEqual(denied["status"], "DENY")
                    await healthy(session)
                    for name in ("read_process_output", "stop_process"):
                        error = await tool(session, name, {"session_id": "f" * 48})
                        assertions.assertEqual(error["status"], "ERROR")
                        assertions.assertEqual(error["error"], "UnknownSession")
                        await healthy(session)
                    if mode == "default":
                        assertions.assertEqual((await tool(session, "run_command", valid))["status"], "DENY")
                    else:
                        review = await tool(session, "run_command", valid | {"argv": ["--version", "PHASE3_PRIVATE_MARKER"]})
                        assertions.assertEqual(review["status"], "REVIEW")
                        await healthy(session)
                        started = await tool(session, "run_command", valid | {"background": True})
                        assertions.assertEqual(started["status"], "RUNNING")
                        assertions.assertEqual(started["decision"], "AUTO")
                        session_id = started["session_id"]
                        while True:
                            output = await tool(session, "read_process_output", {"session_id": session_id})
                            if output["status"] != "RUNNING":
                                break
                            await healthy(session)
                            await anyio.sleep(.01)
                        assertions.assertEqual(output["status"], "COMPLETED")
                        assertions.assertEqual(output["exit_code"], 0)
                        assertions.assertIn("Python", output["stdout"] + output["stderr"])
                        tail = await tool(session, "read_process_output", {"session_id": session_id, "offset": output["next_offset"]})
                        assertions.assertEqual(tail["returned_bytes"], 0)
                        bad_offset = await session.call_tool("read_process_output", {"session_id": session_id, "offset": True})
                        assertions.assertTrue(bad_offset.isError)
                        assertions.assertTrue((await tool(session, "stop_process", {"session_id": session_id}))["already_finished"])
                        failed = await tool(session, "run_command", valid | {"argv": ["--sora-phase3-invalid-option"]})
                        assertions.assertEqual(failed["status"], "FAILED")
                        assertions.assertEqual(failed["decision"], "SCOPED")
                        assertions.assertEqual(failed["exit_code"], 2)
                        await healthy(session)
                        assertions.assertEqual((await tool(session, "read_process_output", {"session_id": session_id}))["status"], "COMPLETED")
                    await healthy(session)
    assertions.assertEqual(len(processes), 0 if memory_transport else 1)
    # stdio_client closes stdin, awaits EOF exit, and escalates only on timeout.
    # Exit 0 proves normal shutdown rather than SDK termination escalation.
    if not memory_transport:
        assertions.assertEqual(processes[0].returncode, 0)
    return {"scenario": mode, "handshake": "PASS", "server_exit": None if memory_transport else 0,
            "transport": "SDK_MEMORY" if memory_transport else "STDIO",
            "protocol": initialized.protocolVersion, "tools": len(listed.tools)}


def run_scenario(mode):
    import anyio
    processes = []

    def watchdog():
        # Independent of the async loop, including SDK cleanup deadlocks.
        for process in processes:
            try:
                if process.returncode is None:
                    process.kill()  # The actual SDK-owned handle, never an arbitrary PID.
            except ProcessLookupError:
                pass
        os._exit(124)

    timer = threading.Timer(30, watchdog)
    timer.start()
    try:
        result = anyio.run(scenario, mode, processes)
        print(json.dumps({"sdk": require_venv_sdk(), **result}), flush=True)
    finally:
        # On assertion failure SDK context cleanup still runs before reaching here.
        for process in processes:
            if process.returncode is None:
                process.kill()
        timer.cancel()
        timer.join()


class RealStdioTests(unittest.TestCase):
    def test_real_sdk_memory_default_separate_from_stdio(self):
        self.check_scenario("memory-default")

    def test_real_sdk_memory_fixture_separate_from_stdio(self):
        self.check_scenario("memory-fixture")

    def test_default_server_real_clientsession(self):
        self.check_scenario("default")

    def test_fixed_fixture_real_clientsession(self):
        self.check_scenario("fixture")

    def check_scenario(self, mode):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", mode],
                                cwd=ROOT, env=ENV, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8", timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("PHASE3_PRIVATE_MARKER", result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence["handshake"], "PASS")
        self.assertEqual(evidence["server_exit"], None if mode.startswith("memory-") else 0)
        print(json.dumps(evidence), flush=True)

    def test_import_has_no_startup_and_closed_stdin_exits(self):
        code = ("import sys; from unittest.mock import patch; "
                "import sora_commander_next.mcp_adapter as adapter; "
                "guard=patch.object(adapter, 'serve_stdio', side_effect=AssertionError('startup')); "
                "guard.start(); import sora_commander_next.stdio_main; "
                "assert 'mcp' not in sys.modules; assert 'tkinter' not in sys.modules")
        imported = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=ENV,
                                  capture_output=True, text=True, timeout=10)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertEqual(imported.stdout, "")
        closed = subprocess.run([sys.executable, "-m", "sora_commander_next.stdio_main"],
                                cwd=ROOT, env=ENV, input="", capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertEqual(closed.stdout, "")


if __name__ == "__main__":
    try:
        require_venv_sdk()
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        if sys.argv[2] not in {"default", "fixture", "memory-default", "memory-fixture"}:
            raise SystemExit("Unknown local scenario")
        run_scenario(sys.argv[2])
    else:
        unittest.main()
