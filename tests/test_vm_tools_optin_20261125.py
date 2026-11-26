"""Guarded VM startup and SDK checks using only synthetic local inputs."""

import asyncio
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mcp.server.fastmcp import FastMCP

from sora_commander_next import (
    allowed_roots, http_local, mcp_adapter, owner_broker_client_main as broker,
    owner_journal_lease, owner_ui_language, product_composition, stdio_main as direct,
    windows_owner,
)
from sora_commander_next.host import Host
from sora_commander_next.owner_broker_dev import DevBrokerHost
from sora_commander_next import vm_control
from tests._inherited_test_temp import inherited_test_temp
from tests.test_vm_control_20261125 import _raw


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
VM = ROOT / "src" / "sora_commander_next"
OPT_IN = "SORA_NEXT_VM_TOOLS"
OWNER_PILOT = "SORA_NEXT_OWNER_REVIEW_PILOT"
PRODUCT = "SORA_NEXT_PRODUCT_BROKER"
NO_CONFIG = "[INFO] VM tools: off (no vm-control.json)\n"
INVALID = "[HOLD] vm-control.json: invalid or unreadable configuration\n"
DEV_REQUIRED = "[HOLD] vm-control.json: DEV Host required\n"
TASKS = "[INFO] Tasks: 0\n"


_CHECK_STOCK_START = """
import os
from pathlib import Path
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, sys.argv[1])
from sora_commander_next import owner_broker_client_main as broker, stdio_main as direct
from sora_commander_next.owner_ui_language import resolve_dev_write_tools

def forbid_vm_file(original):
    def checked(path, *args, **kwargs):
        assert path.name != "vm-control.json", path.name
        return original(path, *args, **kwargs)
    return checked

for module in (direct, broker):
    host = Mock(task_presets=[])
    with patch.object(direct, "Host", return_value=host), \\
            patch.object(broker, "OwnerSessionGuard"), \\
            patch.object(broker, "build_client_host", return_value=host), \\
            patch.object(module, "serve_stdio") as serve, \\
            patch.object(Path, "open", forbid_vm_file(Path.open)), \\
            patch.object(Path, "lstat", forbid_vm_file(Path.lstat)):
        assert resolve_dev_write_tools() is False
        module.main()
        if module is direct:
            serve.assert_called_once_with(host)
        else:
            serve.assert_called_once_with(host, dev_write_tools_enabled=False)
    assert not any(name.startswith("sora_commander_next.vm") or
                   name.startswith("vm_") for name in sys.modules)
"""


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(inherited_test_temp()))
        self.env = {
            "SystemRoot": os.environ["SystemRoot"],
            "USERPROFILE": str(self.home), "LOCALAPPDATA": str(self.home),
        }
        self.enterContext(patch.dict(os.environ, self.env, clear=True))
        self.path = self.home / ".config" / "sora-commander" / "vm-control.json"
        self.path.parent.mkdir(parents=True)
        for field in ("send_folder", "receive_folder"):
            Path(_raw(self.home)[field]).mkdir()
        self.valid = json.dumps(_raw(self.home))

    def test_unconfigured_uses_standard_serve_call_and_reports_info(self):
        for module in (direct, broker):
            with self.subTest(module=module.__name__), patch.dict(os.environ, {OPT_IN: "1"}):
                host = Mock(task_presets=[])
                with patch.object(direct, "Host", return_value=host) as deny_host, \
                        patch.object(broker, "OwnerSessionGuard") as guard, \
                        patch.object(broker, "build_client_host", return_value=host) as client, \
                        patch.object(module, "serve_stdio") as serve, redirect_stderr(StringIO()) as output:
                    module.main()
                if module is direct:
                    deny_host.assert_called_once_with()
                    serve.assert_called_once_with(host)
                    self.assertEqual(output.getvalue(), NO_CONFIG)
                else:
                    client.assert_called_once_with(guard.return_value, ROOT, os.environ)
                    guard.return_value.verify.assert_called_once_with()
                    serve.assert_called_once_with(host, dev_write_tools_enabled=False)
                    self.assertEqual(output.getvalue(), NO_CONFIG + TASKS)

    def test_non_opt_in_starts_without_any_vm_import_or_config_file_access(self):
        self.path.write_text("{broken", encoding="utf-8")
        for value in (None, "0", "true", "1 "):
            with self.subTest(value=value):
                env = self.env if value is None else {**self.env, OPT_IN: value}
                result = subprocess.run(
                    [str(PYTHON), "-E", "-s", "-B", "-c", _CHECK_STOCK_START, str(ROOT / "src")],
                    env=env, cwd=self.home, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, TASKS)

    def test_invalid_or_non_dev_configuration_holds_before_startup_resources(self):
        cases = (
            (direct, "{broken", {OWNER_PILOT: "1"}, INVALID),
            (broker, "{broken", {}, INVALID),
            (direct, self.valid, {PRODUCT: "1"}, DEV_REQUIRED),
            (direct, self.valid, {PRODUCT: "1", OWNER_PILOT: "1"}, DEV_REQUIRED),
            (broker, self.valid, {PRODUCT: "1"}, DEV_REQUIRED),
            (direct, self.valid, {}, DEV_REQUIRED),
        )
        for module, content, env, message in cases:
            with self.subTest(module=module.__name__, env=env, message=message), ExitStack() as stack:
                self.path.write_text(content, encoding="utf-8")
                stack.enter_context(patch.dict(os.environ, {OPT_IN: "1", "SORA_NEXT_JOURNAL_PILOT": "1", **env}))
                output = stack.enter_context(redirect_stderr(StringIO()))
                protected = [
                    stack.enter_context(patch.object(target, name))
                    for target, name in (
                        (direct, "Host"), (direct, "build_owner_pilot_host"),
                        (owner_journal_lease, "owner_journal_lease"),
                        (product_composition, "build_product_host"),
                        (broker, "OwnerSessionGuard"), (broker, "OwnerBrokerPipeClient"),
                        (broker, "DevBrokerHost"), (broker, "build_client_host"),
                        (module, "serve_stdio"),
                    )
                ]
                with self.assertRaises(SystemExit) as caught:
                    module.main()
                self.assertEqual(caught.exception.code, 2)
                self.assertEqual(output.getvalue(), message)
                for resource in protected:
                    resource.assert_not_called()

    def test_configured_original_builders_checks_and_host_close(self):
        self.path.write_text(self.valid, encoding="utf-8")
        expected = vm_control.load_vm_control()
        for module, builder in ((direct, "build_owner_pilot_host"), (broker, "build_client_host")):
            with self.subTest(module=module.__name__), ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {
                    OPT_IN: "1", OWNER_PILOT: "1", "SORA_NEXT_JOURNAL_PILOT": "1",
                }))
                stack.enter_context(redirect_stderr(StringIO()))
                host = Mock(task_presets=[])
                guard = Mock()
                original_serve = module.serve_stdio
                build = stack.enter_context(patch.object(module, builder, return_value=host))
                resolve = stack.enter_context(patch.object(owner_ui_language, "resolve_dev_write_tools", return_value=True))
                lease = stack.enter_context(patch.object(owner_journal_lease, "owner_journal_lease"))
                stack.enter_context(patch.object(broker, "OwnerSessionGuard", return_value=guard))
                stack.enter_context(patch.object(windows_owner, "OwnerSessionGuard", return_value=guard))
                build_mcp = stack.enter_context(patch.object(mcp_adapter, "build_server"))
                register = stack.enter_context(patch.object(vm_control, "register_vm_tools"))
                order = Mock()
                for name, method in (("build", build_mcp), ("register", register),
                                     ("run", build_mcp.return_value.run), ("close", host.close)):
                    order.attach_mock(method, name)
                module.main()
                resolve.assert_called_once_with()
                if module is direct:
                    build.assert_called_once_with()
                    lease.assert_called_once_with(True)
                    lease.return_value.__enter__.assert_called_once_with()
                    lease.return_value.__exit__.assert_called_once_with(None, None, None)
                else:
                    build.assert_called_once_with(guard, ROOT, os.environ)
                    guard.verify.assert_called_once_with()
                    lease.assert_not_called()
                build_mcp.assert_called_once_with(host, dev_write_tools_enabled=True)
                register.assert_called_once_with(build_mcp.return_value, host, expected, guard)
                build_mcp.return_value.run.assert_called_once_with(transport="stdio")
                host.close.assert_called_once_with()
                self.assertEqual([invocation[0] for invocation in order.mock_calls],
                                 ["build", "register", "run", "close"])
                self.assertIs(module.serve_stdio, original_serve)

    def test_registration_failure_still_closes_the_host(self):
        self.path.write_text(self.valid, encoding="utf-8")
        host = Mock(spec=Host)
        with patch.object(mcp_adapter, "build_server") as build, \
                patch.object(windows_owner, "OwnerSessionGuard"), \
                patch.object(vm_control, "register_vm_tools", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaisesRegex(RuntimeError, "^synthetic failure$"):
                mcp_adapter.serve_stdio(host, vm_config=vm_control.load_vm_control())
        build.return_value.run.assert_not_called()
        host.close.assert_called_once_with()


class SurfaceTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(inherited_test_temp()))
        self.enterContext(patch.dict(os.environ, {
            "USERPROFILE": str(self.home), "LOCALAPPDATA": str(self.home), OPT_IN: "1",
        }, clear=True))
        self.config_folder = self.home / ".config" / "sora-commander"
        self.config_folder.mkdir(parents=True)
        for field in ("send_folder", "receive_folder"):
            Path(_raw(self.home)[field]).mkdir()
        (self.config_folder / "vm-control.json").write_text(
            json.dumps(_raw(self.home)), encoding="utf-8",
        )

    def test_build_server_keeps_pinned_counts_and_fingerprints_with_opt_in(self):
        from tools import scn_muse_inbox
        from tools.scn_muse_digest import register_digest

        cases = (
            (False, False, False, 43, "eefa7ed96bd6a464fa93de551aa9bf76a9a5b2ccdd07889086cc677e2e5b263c"),
            (True, False, False, 49, "99513c27b4c0fcae7f6dd0fbe0f3dc4154bc2eb900966f2561bb25ac6dab61c4"),
            (False, True, False, 36, "4545c02b518b9532757aece287176c6453534edb415b2bd600313d446cab9171"),
            (False, True, True, 37, "85be1502a93dece3c664d159995ec8911f211ceb4b15a140f3428b27c8945840"),
        )
        for pilot, readonly, inbox, count, fingerprint in cases:
            with self.subTest(pilot=pilot, readonly=readonly, inbox=inbox):
                host = Host()
                self.addCleanup(host.close)
                if pilot:
                    host._edit_review_pilot = SimpleNamespace(enable_create=True)
                flags = (dict(dev_writes_enabled=False, dev_tools_enabled=False,
                              command_tools_enabled=False) if readonly else {})
                server = mcp_adapter.build_server(host, **flags)
                if readonly:
                    register_digest(server, host, checkout=ROOT, auth="tailscale", inbox=inbox)
                if inbox:
                    with patch.object(scn_muse_inbox, "_check_folder"):
                        scn_muse_inbox.register_inbox(server, host._activity_store, str(ROOT))
                self.assertEqual(len(asyncio.run(server.list_tools())), count)
                catalog = mcp_adapter._local_registered_catalog(server, host)
                self.assertEqual(catalog["tool_schema_sha256"], fingerprint)

    def test_http_local_and_muse_register_no_vm_tools_even_when_configured(self):
        from tools import scn_muse_bridge
        import uvicorn

        captured = []
        build_server = mcp_adapter.build_server

        def capture(*args, **kwargs):
            server = build_server(*args, **kwargs)
            captured.append(server)
            return server

        http_host = Host()
        self.addCleanup(http_host.close)
        with patch.object(http_local, "Host", return_value=http_host), \
                patch.object(http_local, "build_server", side_effect=capture):
            http_local.create_loopback_app(secrets.token_hex(32))

        roots_file = self.config_folder / "allowed-roots.txt"
        roots_file.touch()
        with patch.object(mcp_adapter, "build_server", side_effect=capture), \
                patch.object(allowed_roots, "startup_allowed_roots",
                             return_value=SimpleNamespace(read=(str(self.home),))), \
                patch.object(scn_muse_bridge, "build_exec_host", side_effect=lambda *a, **k: Host()), \
                patch.object(scn_muse_bridge, "transport_lists", return_value=([], [])), \
                patch.object(uvicorn, "run") as run, redirect_stdout(StringIO()):
            args = SimpleNamespace(bind="fixture", port=19001, allow_magicdns=False,
                                   enable_dev_writes=True, enable_edit_pilot=True, enable_create_pilot=True)
            for mode in ("readonly", "exec"):
                args.mode = mode
                self.assertEqual(scn_muse_bridge._serve_bridge(args, ROOT, secrets.token_hex(32), "token", None), 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(len(captured), 3)
        for kind, server in zip(("http_local", "Muse readonly", "Muse exec"), captured, strict=True):
            with self.subTest(kind=kind):
                self.assertFalse(any(tool.name.startswith("vm_") for tool in asyncio.run(server.list_tools())))

    def test_stdio_config_adds_exactly_nine_vm_tools_and_serves_the_sdk_call(self):
        host = Mock(spec=Host)
        close = host.close
        ordinary = mcp_adapter.build_server(host)
        config = mcp_adapter.startup_vm_config(dev_host=True)
        self.assertIsInstance(config, vm_control.VmConfig)
        servers = []
        with patch.object(windows_owner, "OwnerSessionGuard"), \
                patch.object(FastMCP, "run", autospec=True,
                             side_effect=lambda server, **kwargs: servers.append(server)) as run:
            mcp_adapter.serve_stdio(host, vm_config=config)
        server, = servers
        run.assert_called_once_with(server, transport="stdio")
        existing = {tool.name: tool for tool in asyncio.run(ordinary.list_tools())}
        configured = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        self.assertEqual(set(configured) - set(existing), {
            "vm_session_start", "vm_session_status", "vm_look", "vm_act", "vm_file_list",
            "vm_file_put", "vm_file_get", "vm_file_status", "vm_file_cancel",
        })
        self.assertEqual({name: configured[name] for name in existing}, existing)
        _, result = asyncio.run(server.call_tool("vm_session_status", {}))
        self.assertEqual((result["state"], result["agent"]), ("NONE", "NOT_CHECKED"))
        close.assert_called_once_with()

    def test_direct_and_broker_stdio_eof_release_only_the_client_vm_indicator(self):
        config = mcp_adapter.startup_vm_config(dev_host=True)
        for kind in (Host, DevBrokerHost):
            with self.subTest(kind=kind.__name__):
                host = Mock(spec=kind)
                events = []
                host.run_command_wait.return_value = {"status": "PENDING", "pending_id": "a" * 32}
                host.close.side_effect = lambda: events.append("host")
                server = FastMCP("test")

                def run(**_):
                    _, result = asyncio.run(server.call_tool("vm_session_start", {"wait_seconds": 0}))
                    self.assertEqual(result["status"], "PENDING")

                with patch.object(mcp_adapter, "build_server", return_value=server), \
                        patch.object(windows_owner, "OwnerSessionGuard"), patch.object(server, "run", run), \
                        patch.object(vm_control.vm_lease, "hold_indicator",
                                     return_value=lambda: events.append("indicator")):
                    mcp_adapter.serve_stdio(host, vm_config=config)
                self.assertEqual(events, ["indicator", "host"])
                host.run_command_wait.assert_called_once()
                host.stop_process.assert_not_called()


class FlatGuestAndLauncherTests(unittest.TestCase):
    def test_five_flat_guest_files_start_without_package_imports(self):
        home = Path(self.enterContext(inherited_test_temp()))
        env = {"USERPROFILE": str(home), "LOCALAPPDATA": str(home)}
        names = {"vm_agent.py", "vm_protocol.py", "vm_win.py", "vm_files.py", "vm_uia.py"}
        self.assertEqual(list(home.iterdir()), [])
        for name in names:
            (home / name).write_bytes((VM / name).read_bytes())
        self.assertEqual({item.name for item in home.iterdir()}, names)
        result = subprocess.run(
            [str(PYTHON), "-E", "-s", "-B", "-v", "vm_agent.py", "--help"],
            cwd=home, env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: vm_agent.py", result.stdout)
        self.assertNotIn("import 'sora_commander_next", result.stderr)

    def test_vm_launchers_opt_in_and_stock_launchers_clear_inherited_opt_in(self):
        for base, module in (("mcp-direct", "stdio_main"), ("mcp-broker-client", "owner_broker_client_main")):
            with self.subTest(launcher=base):
                stock = (ROOT / "tools" / f"{base}.cmd").read_bytes()
                enabled = (ROOT / "tools" / f"{base}-vm.cmd").read_bytes()
                self.assertEqual(stock.splitlines().count(b'set "SORA_NEXT_VM_TOOLS="'), 1)
                self.assertEqual(enabled.splitlines().count(b'set "SORA_NEXT_VM_TOOLS=1"'), 1)
                self.assertEqual(enabled, stock.replace(b'SORA_NEXT_VM_TOOLS="', b'SORA_NEXT_VM_TOOLS=1"'))
                self.assertIn(f"-m sora_commander_next.{module}\r\n".encode(), enabled)
        broker_script = (ROOT / "tools" / "start-broker.cmd").read_bytes()
        self.assertEqual(broker_script.splitlines().count(b'set "SORA_NEXT_VM_TOOLS="'), 1)
        tunnel = (ROOT / "tools" / "start-vm-tunnel.cmd").read_bytes()
        self.assertIn(b'for %%I in ("%~dp0..") do set "BASE=%%~fI"\r\n', tunnel)
        self.assertIn(b'set "MCP_START=%BASE%\\tools\\mcp-broker-client-vm.cmd"\r\n', tunnel)

    def test_launchers_keep_crlf(self):
        for name in ("mcp-direct", "mcp-direct-vm", "mcp-broker-client", "mcp-broker-client-vm",
                     "start-broker", "start-vm-tunnel"):
            with self.subTest(launcher=name):
                data = (ROOT / "tools" / f"{name}.cmd").read_bytes()
                self.assertTrue(data.endswith(b"\r\n"))
                without_crlf = data.replace(b"\r\n", b"")
                self.assertNotIn(b"\n", without_crlf)
                self.assertNotIn(b"\r", without_crlf)
