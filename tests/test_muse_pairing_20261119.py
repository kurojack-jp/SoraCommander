"""Synthetic state, startup and MCP-over-ASGI tests for Muse exec pairing."""

from contextlib import nullcontext, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from mcp.server.fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import build_server
from sora_commander_next.policy import Policy
from tools import scn_muse_bridge as bridge
from tools.scn_muse_digest import register_digest
from tools.scn_muse_inbox import register_inbox
from tools.scn_muse_pairing import Pairing, register_pairing


ROOT = Path(__file__).resolve().parents[1]
BIND = "100.64.0.1"
CLIENT = "100.64.0.2"
PEER = "synthetic-peer-first1"
OTHER_PEER = "synthetic-peer-second"
CODE = "ABCD-2345"
STARTUP_LINE = (
    f"[bridge] pairing code: {CODE} "
    "(valid 10 minutes, one use; never share it except with your agent)"
)


def new_pairing(clock=lambda: 0):
    return Pairing(clock=clock, wall=lambda: datetime(2026, 11, 19, tzinfo=UTC),
                   rng=Mock(choice=Mock(side_effect=list(CODE.replace("-", "")))))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.logs = self.enterContext(redirect_stdout(io.StringIO()))
        self.now = 0
        self.pairing = new_pairing(lambda: self.now)

    def test_normalized_code_is_single_use_and_binds_only_the_caller(self):
        self.assertEqual(self.pairing.display(), CODE)
        self.assertFalse(self.pairing.allows(PEER))
        self.assertEqual(self.pairing.pair(" abcd-2345 ", PEER),
                         {"status": "PAIRED", "until": "2026-11-19T08:00:00Z"})
        self.assertEqual(self.pairing.pair(CODE, OTHER_PEER), {"status": "PAIRING_LOCKED"})
        self.assertTrue(self.pairing.allows(PEER))
        self.assertFalse(self.pairing.allows(OTHER_PEER))
        self.assertIn(PEER[-6:], self.logs.getvalue())
        for secret in (PEER, CODE, CODE.replace("-", "")):
            self.assertNotIn(secret, self.logs.getvalue())

    def test_five_failures_include_invalid_types_and_lengths(self):
        for invalid in (1, "x" * 33, "??", "誤り", "0000-0000"):
            self.assertEqual(self.pairing.pair(invalid, PEER), {"status": "PAIRING_FAILED"})
        for _ in range(2):
            self.assertEqual(self.pairing.pair(CODE, PEER), {"status": "PAIRING_LOCKED"})
        self.assertEqual(self.logs.getvalue().count("pairing failed"), 5)
        self.assertEqual(self.logs.getvalue().count("pairing locked"), 1)
        self.assertFalse(self.pairing.allows(PEER))

    def test_code_expires_at_ten_minutes_and_logs_once(self):
        self.now = 600
        for _ in range(2):
            self.assertEqual(self.pairing.pair(CODE, PEER), {"status": "PAIRING_EXPIRED"})
        self.assertEqual(self.logs.getvalue().count("pairing code expired"), 1)
        self.assertFalse(self.pairing.allows(PEER))

    def test_binding_expires_eight_hours_after_pairing_and_logs_once(self):
        self.now = 599
        self.assertEqual(self.pairing.pair("abcd2345", PEER)["status"], "PAIRED")
        self.now += 28799
        self.assertTrue(self.pairing.allows(PEER))
        self.now += 1
        self.assertFalse(self.pairing.allows(OTHER_PEER))
        self.assertFalse(self.pairing.allows(PEER))
        self.assertEqual(self.logs.getvalue().count("binding expired"), 1)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.logs = self.enterContext(redirect_stdout(io.StringIO()))
        self.errors = self.enterContext(redirect_stderr(io.StringIO()))
        self.enterContext(patch.object(sys, "path", list(sys.path)))
        self.enterContext(patch.dict("os.environ", {"USERPROFILE": str(ROOT / "synthetic-owner")}))
        self.host = Host(policy=Policy(roots=()))
        self.addCleanup(self.host.close)
        self.command = self.enterContext(patch.object(self.host, "run_command_wait",
                                                     return_value={"status": "COMPLETED"}))
        self.identity = Mock(spec=bridge.PeerIdentity)
        self.identity.lookup.return_value = PEER
        self.token = self.enterContext(patch.object(bridge, "load_exec_token"))
        self.peers = self.enterContext(patch.object(bridge, "load_peers", return_value={
            "readonly": ["read-node"], "exec": [PEER, OTHER_PEER]}))
        self.enterContext(patch.object(bridge, "PeerIdentity", return_value=self.identity))
        self.compose = self.enterContext(patch.object(bridge, "build_exec_host", return_value=self.host))
        self.enterContext(patch("sora_commander_next.owner_journal_lease.owner_journal_lease",
                                return_value=nullcontext()))
        self.enterContext(patch("tools.scn_muse_pairing.Pairing", return_value=new_pairing()))
        self.run_listener = self.enterContext(patch("uvicorn.run"))
        self.args = ["--checkout", str(ROOT), "--bind", BIND]

    def test_incompatible_auth_modes_are_parser_errors(self):
        for mode, auth in (("exec", "token"), ("exec", "tailscale"), ("readonly", "pairing")):
            with self.subTest(mode=mode, auth=auth), self.assertRaises(SystemExit) as failure:
                bridge.main(self.args + ["--mode", mode, "--auth", auth])
            self.assertEqual(failure.exception.code, 2)
        self.compose.assert_not_called()
        self.peers.assert_not_called()
        self.token.assert_not_called()
        self.run_listener.assert_not_called()

    def test_invalid_or_empty_exec_peers_hold_without_token_read(self):
        for result, error in ((None, FileNotFoundError()), (None, ValueError()),
                              ({"exec": [], "readonly": ["read-node"]}, None)):
            self.peers.return_value, self.peers.side_effect = result, error
            self.assertEqual(bridge.main(self.args + ["--mode", "exec", "--auth", "pairing"]), 2)
        self.assertEqual(self.errors.getvalue().count("Peer allow-list missing, invalid or empty"), 3)
        self.token.assert_not_called()
        self.compose.assert_not_called()
        self.run_listener.assert_not_called()

    def test_pairing_over_real_mcp_http(self):
        self.assertEqual(bridge.main(self.args + ["--mode", "exec", "--auth", "pairing",
                                                 "--token-file", "ignored-token-file"]), 0)
        self.token.assert_not_called()
        self.peers.assert_called_once()
        bridge.PeerIdentity.assert_called_once_with([PEER, OTHER_PEER])
        self.run_listener.assert_called_once()
        gate = self.run_listener.call_args.args[0]
        self.assertIsInstance(gate, bridge.TokenGate)
        self.assertEqual(gate.auth, "pairing")
        self.assertIn("[bridge] mode: EXEC with one-time pairing", self.logs.getvalue())
        self.assertEqual([line for line in self.logs.getvalue().splitlines() if CODE in line], [STARTUP_LINE])
        headers = {"Accept": "application/json, text/event-stream"}
        request_id = 0

        with TestClient(gate, base_url=f"http://{BIND}:8765", client=(CLIENT, 12345)) as client:
            def rpc(method, params=None, *, notification=False):
                nonlocal request_id
                request_id += 1
                packet = {"jsonrpc": "2.0", "method": method, "params": params or {}}
                if not notification:
                    packet["id"] = request_id
                response = client.post("/mcp", headers=headers, json=packet)
                if notification:
                    self.assertEqual(response.status_code, 202, response.text)
                    return
                self.assertEqual(response.status_code, 200, response.text)
                if response.headers.get("mcp-session-id"):
                    headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
                if response.headers["content-type"].startswith("text/event-stream"):
                    data = next(line.removeprefix("data: ") for line in response.text.splitlines()
                                if line.startswith("data: "))
                    message = json.loads(data)
                else:
                    message = response.json()
                self.assertEqual(message["id"], request_id)
                self.assertIn("result", message)
                return message["result"]

            def call(name, arguments):
                result = rpc("tools/call", {"name": name, "arguments": arguments})
                self.assertFalse(result.get("isError", False), result)
                return result["structuredContent"]

            init = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                      "clientInfo": {"name": "synthetic-muse", "version": "1"}})
            headers["MCP-Protocol-Version"] = init["protocolVersion"]
            rpc("notifications/initialized", notification=True)
            names = {tool["name"] for tool in rpc("tools/list")["tools"]}
            default_server = build_server(self.host, dev_writes_enabled=False)
            default_names = {tool.name for tool in default_server._tool_manager.list_tools()}
            self.assertNotIn("exec_pair", default_names)
            self.assertEqual(names, default_names | {"exec_pair"})
            print(f"MUSE_EXEC_TOOL_COUNTS both={len(default_names)} pairing={len(names)}",
                  file=sys.__stdout__, flush=True)

            command = {"executable": sys.executable, "argv": ["--version"], "cwd": str(ROOT)}
            for name in sorted(names - {"exec_pair"}):
                with self.subTest(tool=name):
                    self.assertEqual(call(name, command if name == "run_command_wait" else {}),
                                     {"status": "PAIRING_REQUIRED"})
            self.command.assert_not_called()
            self.assertEqual(call("exec_pair", {"code": "0000-0000"}), {"status": "PAIRING_FAILED"})
            self.assertEqual(call("exec_pair", {"code": CODE}),
                             {"status": "PAIRED", "until": "2026-11-19T08:00:00Z"})
            self.assertEqual(call("run_command_wait", command), {"status": "COMPLETED"})
            self.command.assert_called_once()
            self.identity.lookup.return_value = OTHER_PEER
            self.assertEqual(call("run_command_wait", command), {"status": "PAIRING_REQUIRED"})
            self.command.assert_called_once()
            self.identity.lookup.return_value = None
            rejected = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 999, "method": "tools/list", "params": {}})
            self.assertEqual(rejected.status_code, 401)
            self.assertEqual(rejected.json(), {"error": "unauthorized"})
        self.identity.lookup.assert_called_with(CLIENT)
        self.assertEqual([line for line in self.logs.getvalue().splitlines() if CODE in line], [STARTUP_LINE])
        self.assertNotIn(CODE.replace("-", ""), self.logs.getvalue())

    def test_default_exec_auth_requires_both_factors(self):
        token = "synthetic-exec-credential-" * 2
        self.token.return_value = token
        self.assertEqual(bridge.main(self.args + ["--mode", "exec"]), 0)
        self.token.assert_called_once()
        gate = self.run_listener.call_args.args[0]
        self.assertEqual(gate.auth, "both")
        packet = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "synthetic-muse", "version": "1"}}}
        headers = {"Accept": "application/json, text/event-stream"}
        with TestClient(gate, base_url=f"http://{BIND}:8765", client=(CLIENT, 12345)) as client:
            self.assertEqual(client.post("/mcp", headers=headers, json=packet).status_code, 401)
            self.identity.lookup.assert_not_called()
            headers["X-Sora-Bridge-Token"] = token
            self.identity.lookup.return_value = None
            self.assertEqual(client.post("/mcp", headers=headers, json=packet).status_code, 401)
            self.identity.lookup.return_value = PEER
            self.assertEqual(client.post("/mcp", headers=headers, json=packet).status_code, 200)
        self.command.assert_not_called()

    def test_readonly_tool_counts_and_pairing_absence(self):
        for inbox in (False, True):
            with self.subTest(inbox=inbox):
                server = build_server(self.host, dev_writes_enabled=False, dev_tools_enabled=False)
                register_digest(server, self.host, checkout=ROOT, auth="tailscale", inbox=inbox)
                if inbox:
                    with patch("tools.scn_muse_inbox.Inbox"):
                        register_inbox(server, None, "synthetic-profile")
                names = {tool.name for tool in server._tool_manager.list_tools()}
                self.assertEqual(len(names), 43 if inbox else 42)
                self.assertNotIn("exec_pair", names)


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def test_pairing_wire_validation_and_missing_request_context(self):
        host = Mock(spec=Host)
        server = build_server(host, dev_writes_enabled=False)
        register_pairing(server, new_pairing())
        for arguments in ({}, {"code": 123}, {"code": "x" * 33}, {"code": ""},
                          {"code": CODE, "extra": True}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                await server.call_tool("exec_pair", arguments)
        _, result = await server.call_tool("exec_pair", {"code": CODE})
        self.assertEqual(result, {"status": "PAIRING_REQUIRED"})
        refusal = await server.call_tool("run_command_wait", {})
        self.assertEqual(refusal.structuredContent, {"status": "PAIRING_REQUIRED"})
        host.run_command_wait.assert_not_called()
