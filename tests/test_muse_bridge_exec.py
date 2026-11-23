"""Offline Muse exec contracts. No sockets, real Owner GUI, jobs or credentials."""
import asyncio
from contextlib import nullcontext, redirect_stdout, redirect_stderr
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests._inherited_test_temp import inherited_test_temp
from tools import scn_muse_bridge as bridge
from sora_commander_next.allowed_roots import AllowedRoots
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy

ROOT = Path(__file__).resolve().parents[1]
EXEC_TOKEN = "exec-synthetic-" + "e" * 48
READ_TOKEN = "read-synthetic-" + "r" * 48


class TokenGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_websocket_scope_is_closed_without_reaching_app(self):
        app, send = AsyncMock(), AsyncMock()
        await bridge.TokenGate(app, READ_TOKEN)({"type": "websocket"}, Mock(), send)
        send.assert_awaited_once_with({"type": "websocket.close", "code": 1008})
        app.assert_not_called()

    async def test_lifespan_scope_reaches_app(self):
        app, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
        scope = {"type": "lifespan"}
        await bridge.TokenGate(app, READ_TOKEN)(scope, receive, send)
        app.assert_awaited_once_with(scope, receive, send)

    async def test_unknown_scope_is_dropped(self):
        app, send = AsyncMock(), AsyncMock()
        await bridge.TokenGate(app, READ_TOKEN)({"type": "other"}, Mock(), send)
        app.assert_not_called()
        send.assert_not_called()

    async def test_exec_missing_wrong_and_readonly_token_are_401(self):
        calls = []

        async def app(scope, receive, send):
            calls.append(scope)

        for presented in (None, b"wrong", READ_TOKEN.encode(), EXEC_TOKEN.encode()):
            messages = []

            async def send(message):
                messages.append(message)

            headers = [] if presented is None else [(bridge.TOKEN_HEADER, presented)]
            await bridge.TokenGate(app, EXEC_TOKEN)({"type": "http", "headers": headers}, Mock(), send)
            if presented == EXEC_TOKEN.encode():
                self.assertEqual(len(calls), 1)
            else:
                self.assertEqual(messages[0]["status"], 401)
                self.assertEqual(calls, [])
                self.assertNotIn(EXEC_TOKEN.encode(), messages[-1]["body"])


@unittest.skipUnless(os.name == "nt", "Windows synthetic paths")
class CredentialTests(unittest.TestCase):
    def test_exec_file_is_required_even_when_readonly_env_is_present(self):
        with inherited_test_temp() as folder:
            home = Path(folder) / "profile"
            config = home / ".config/sora-commander"
            config.mkdir(parents=True)
            path = config / bridge.EXEC_TOKEN_NAME
            env = {"USERPROFILE": str(home), "SORA_BRIDGE_TOKEN": READ_TOKEN}
            with self.assertRaises(ValueError):
                bridge.load_exec_token(path, env)
            path.write_text(EXEC_TOKEN, encoding="utf-8")
            self.assertEqual(bridge.load_exec_token(path, env), EXEC_TOKEN)
            readonly = config / "muse-bridge-token.txt"
            readonly.write_text(EXEC_TOKEN, encoding="utf-8")
            with self.assertRaises(ValueError):
                bridge.load_exec_token(path, env)
            readonly.write_text(READ_TOKEN, encoding="utf-8")
            with self.assertRaises(ValueError):
                bridge.load_exec_token(readonly, env)
            with self.assertRaises(ValueError):
                bridge.load_exec_token(path, dict(env, SORA_BRIDGE_TOKEN=EXEC_TOKEN))

    def test_readonly_token_file_uses_the_bounded_reader(self):
        with inherited_test_temp() as folder, patch.dict(os.environ, {"SORA_BRIDGE_TOKEN": ""}):
            path = Path(folder) / "readonly-token.txt"
            with self.assertRaises(SystemExit):
                bridge.load_token(path)  # missing
            path.write_text("# comment\n" + READ_TOKEN + "\nignored\n", encoding="utf-8")
            self.assertEqual(bridge.load_token(path), READ_TOKEN)
            with patch.dict(os.environ, {"SORA_BRIDGE_TOKEN": EXEC_TOKEN}):
                self.assertEqual(bridge.load_token(path), EXEC_TOKEN)  # env still wins
            for value in ("short", "x" * 4097):
                path.write_text(value, encoding="utf-8")
                with self.subTest(length=len(value)), self.assertRaises(SystemExit):
                    bridge.load_token(path)
            path.write_text(READ_TOKEN, encoding="utf-8")
            other = Path(folder) / "second-name.txt"
            os.link(path, other)  # a second hard link makes it non-ordinary
            try:
                with self.assertRaises(SystemExit) as caught:
                    bridge.load_token(path)
                self.assertNotIn(READ_TOKEN, str(caught.exception))
            finally:
                # The fixture refuses to clean up multi-link files; drop the
                # extra link before leaving it.
                other.unlink()

    def test_malformed_exec_token_is_refused(self):
        with inherited_test_temp() as folder:
            home = Path(folder) / "profile"
            path = home / ".config/sora-commander" / bridge.EXEC_TOKEN_NAME
            path.parent.mkdir(parents=True)
            for value in ("short", EXEC_TOKEN + "\nsecond", "x" * 4097, "x" * 40 + "\0", "あ" * 40):
                path.write_text(value, encoding="utf-8")
                with self.subTest(length=len(value)), self.assertRaises(ValueError):
                    bridge.load_exec_token(path, {"USERPROFILE": str(home)})


class MainTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("sora_commander_next.owner_journal_lease.owner_journal_lease",
                                side_effect=lambda enabled=True: nullcontext()))
        self.enterContext(patch.object(sys, "path", list(sys.path)))
        self.output = self.enterContext(redirect_stdout(io.StringIO()))
        self.errors = self.enterContext(redirect_stderr(io.StringIO()))
        self.run = Mock()
        self.enterContext(patch.object(bridge, "example_peer_ids", return_value=frozenset()))
        self.enterContext(patch.object(bridge, "load_peers", return_value={
            "readonly": ["read-node"], "exec": ["exec-node"]}))
        self.security = Mock(side_effect=lambda **kw: SimpleNamespace(**kw))
        # Do not restore all of sys.modules around SDK imports: doing so can
        # leave Pydantic classes bound to different module generations.
        self.enterContext(patch("uvicorn.run", self.run))
        self.enterContext(patch("mcp.server.transport_security.TransportSecuritySettings", self.security))
        self.argv = ["--checkout", str(ROOT), "--bind", "100.64.0.1"]
        self.profile = ROOT / "synthetic-owner-profile"
        self.enterContext(patch.dict(os.environ, {"USERPROFILE": str(self.profile),
                                                  "HOME": str(ROOT / "different-home")}))
        self.config = Mock(spec=Path)
        self.locations = self.enterContext(patch("sora_commander_next.allowed_roots._locations",
                                                 return_value=(self.profile, self.config, (), ())))
        self.server = SimpleNamespace(settings=SimpleNamespace(), streamable_http_app=Mock(return_value=Mock()),
                                      tool=Mock(return_value=lambda function: function))
        self.built = self.enterContext(patch("sora_commander_next.mcp_adapter.build_server", return_value=self.server))

    def test_readonly_token_mode_host_and_banner_are_preserved(self):
        roots = AllowedRoots((str(ROOT),), (str(ROOT / "docs"),))
        with patch("sora_commander_next.allowed_roots.startup_allowed_roots", return_value=roots), \
                patch.object(bridge, "load_token", return_value=READ_TOKEN) as load:
            self.assertEqual(bridge.main(self.argv + ["--auth", "token"]), 0)
        load.assert_called_once_with(self.profile / ".config/sora-commander/muse-bridge-token.txt")
        host = self.built.call_args.args[0]
        self.assertEqual(host.policy.roots, ())
        self.assertEqual(host.policy.read_roots, roots.read)
        self.assertIsNone(host._owner_review_dispatcher)
        result = host.run_command_wait(executable=sys.executable, argv=["--version"], cwd=str(ROOT), wait_seconds=0)
        self.assertEqual(result["status"], "DENY")
        self.assertIn("Sora Commander read-only bridge for Muse", self.output.getvalue())
        self.assertNotIn("mode: EXEC", self.output.getvalue())
        self.built.assert_called_once_with(host, dev_writes_enabled=False, dev_tools_enabled=False,
                                          command_tools_enabled=False)
        self.server.tool.assert_called_once_with()
        self.assertEqual(self.run.call_args.kwargs["ws"], "none")
        self.security.assert_called_once_with(enable_dns_rebinding_protection=True,
            allowed_hosts=["100.64.0.1:8765", "100.64.0.1"], allowed_origins=["http://100.64.0.1:8765"])

    def test_missing_allowed_roots_file_prevents_startup(self):
        self.config.lstat.side_effect = FileNotFoundError()
        with patch.object(bridge, "load_token", return_value=READ_TOKEN), \
                patch("sora_commander_next.allowed_roots.startup_allowed_roots") as startup:
            self.assertEqual(bridge.main(self.argv), 2)
        self.assertEqual(self.errors.getvalue().strip(),
                         "[HOLD] allowed-roots.txt is missing; the read-only bridge serves only folders you list. Bridge not started.")
        startup.assert_not_called()
        self.built.assert_not_called()
        self.run.assert_not_called()

    def test_allowed_roots_location_or_inspection_failure_prevents_startup(self):
        with patch.object(bridge, "load_token", return_value=READ_TOKEN), \
                patch("sora_commander_next.allowed_roots.startup_allowed_roots") as startup:
            self.config.lstat.side_effect = PermissionError("synthetic")
            self.assertEqual(bridge.main(self.argv), 2)
            self.locations.side_effect = ValueError("synthetic")
            self.assertEqual(bridge.main(self.argv), 2)
        self.assertEqual(self.errors.getvalue().splitlines(),
                         ["[HOLD] allowed-roots.txt could not be checked. Bridge not started."] * 2)
        startup.assert_not_called()
        self.built.assert_not_called()
        self.run.assert_not_called()

    def test_invalid_allowed_roots_line_is_one_hold_line(self):
        from sora_commander_next.allowed_roots import AllowedRootsError
        failure = AllowedRootsError(3, "protected system or user folder")
        with patch.object(bridge, "load_token", return_value=READ_TOKEN),                 patch("sora_commander_next.allowed_roots.startup_allowed_roots", side_effect=failure):
            self.assertEqual(bridge.main(self.argv + ["--auth", "token"]), 2)
        self.assertEqual(self.errors.getvalue().splitlines(),
                         ["[HOLD] allowed-roots.txt: allowed-roots line 3: protected system or user folder. "
                          "Bridge not started."])
        self.built.assert_not_called()
        self.run.assert_not_called()

    def test_missing_checkout_is_one_hold_line(self):
        argv = ["--checkout", str(ROOT / "synthetic-missing-checkout"), "--bind", "100.64.0.1",
                "--auth", "token"]
        self.assertEqual(bridge.main(argv), 2)
        self.assertEqual(self.errors.getvalue().splitlines(),
                         ["[HOLD] The --checkout folder was not found. Bridge not started."])
        self.run.assert_not_called()

    def test_exec_construction_failure_never_listens_or_falls_back(self):
        for failure in (PermissionError("synthetic"), RuntimeError("synthetic"), SystemExit(2)):
            with patch.object(bridge, "load_exec_token", return_value=EXEC_TOKEN), \
                    patch.object(bridge, "build_exec_host", side_effect=failure):
                self.assertEqual(bridge.main(self.argv + ["--mode", "exec"]), 2)
        self.built.assert_not_called()
        self.run.assert_not_called()
        self.assertNotIn(EXEC_TOKEN, self.errors.getvalue())

    def test_exec_default_writers_off_and_network_settings_unchanged(self):
        host = SimpleNamespace(policy=SimpleNamespace(roots=("full",), read_only_roots=("read",)), close=Mock())
        with patch.object(bridge, "load_exec_token", return_value=EXEC_TOKEN), \
                patch.object(bridge, "build_exec_host", return_value=host) as compose:
            self.assertEqual(bridge.main(self.argv + ["--mode", "exec"]), 0)
        compose.assert_called_once_with(ROOT, enable_edit=False, enable_create=False)
        self.built.assert_called_once_with(host, dev_writes_enabled=False,
                                           dev_write_tools_enabled=False)
        self.server.tool.assert_not_called()
        self.assertIsInstance(self.run.call_args.args[0], bridge.TokenGate)
        self.assertEqual(self.run.call_args.args[0].auth, "both")
        self.assertEqual(self.run.call_args.args[0].identity.allowed, frozenset({"exec-node"}))
        self.assertEqual(self.run.call_args.kwargs["port"], 8765)
        self.assertIs(self.run.call_args.kwargs["access_log"], False)
        self.assertEqual(self.run.call_args.kwargs["log_level"], "info")
        self.assertIn("1 full, 1 read-only", self.output.getvalue())
        self.assertNotIn(EXEC_TOKEN, self.output.getvalue())

    def test_bad_bind_and_exec_port_never_start(self):
        for extra in (["--bind", "127.0.0.1"], ["--bind", "0.0.0.0"],
                      ["--mode", "exec", "--port", "8766"], ["--enable-dev-writes"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                bridge.main(self.argv + extra)
        self.run.assert_not_called()


class ReadonlyCommandWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_hidden_command_is_refused_without_calling_host(self):
        from mcp.server.fastmcp.exceptions import ToolError
        from sora_commander_next.mcp_adapter import build_server

        host = Host(policy=Policy(roots=()))
        self.addCleanup(host.close)
        server = build_server(host, dev_writes_enabled=False, dev_tools_enabled=False,
                              command_tools_enabled=False)
        with patch.object(host, "run_command_wait") as run:
            with self.assertRaises(ToolError):
                await server.call_tool("run_command_wait", {
                    "executable": sys.executable, "argv": [], "cwd": str(ROOT),
                    "wait_seconds": 0,
                })
        run.assert_not_called()

    def test_command_selection_requires_an_explicit_boolean(self):
        from sora_commander_next.mcp_adapter import build_server

        with self.assertRaisesRegex(ValueError, "explicit boolean command tool"):
            build_server(object(), command_tools_enabled=0)


@unittest.skipUnless(os.name == "nt", "Owner composition uses Windows modules")
class CompositionTests(unittest.TestCase):
    def test_readonly_catalog_excludes_dev_tools_and_interactive_install(self):
        from sora_commander_next.mcp_adapter import build_server
        from tools.scn_muse_digest import register_digest

        host = Host(policy=Policy(roots=(), read_only_roots=(str(ROOT),)))
        self.addCleanup(host.close)
        with patch("sora_commander_next.dev_rdc_compat.install_host_interactive") as install:
            server = build_server(host, dev_writes_enabled=False, dev_tools_enabled=False,
                                  command_tools_enabled=False)
            register_digest(server, host, checkout=ROOT, auth="tailscale", inbox=False)
        names = {tool.name for tool in server._tool_manager.list_tools()}
        self.assertTrue({"status", "read_text_file", "path_info", "list_folder", "bridge_digest"} <= names)
        self.assertTrue(names.isdisjoint({"send_input", "create_directory", "create_file", "move_file",
                                          "request_source_patch_dev", "list_processes", "get_config"}))
        self.assertTrue({"list_tasks", "activity", "request_status"} <= names)
        self.assertTrue(names.isdisjoint({"run_command", "run_command_wait", "run_task", "ogf_cue",
                                          "await_command_result", "review_status"}))
        self.assertEqual(len(names), 36)
        install.assert_not_called()

    def test_shared_composition_keeps_scopes_and_mandatory_dispatcher(self):
        from sora_commander_next import stdio_main, windows_owner_controller, owner_review_dispatch, windows_owner

        class Dispatcher:
            fail = False

            def __init__(self, controller):
                if self.fail:
                    raise RuntimeError("synthetic dispatcher failure")
                self._controller = controller

        with patch.dict(os.environ, {"SORA_NEXT_PRODUCT_BROKER": "0", "SORA_NEXT_EDIT_PILOT": "1"}), \
                patch.object(windows_owner, "OwnerSessionGuard") as guard_type, \
                patch.object(stdio_main, "startup_allowed_roots", return_value=AllowedRoots((str(ROOT / "src"),), (str(ROOT / "docs"),))), \
                patch("sora_commander_next.owner_ui_language.owner_ui_language", return_value="ja"), \
                patch("sora_commander_next.task_presets.load_task_presets", return_value=("preset",)), \
                patch("sora_commander_next.ogf_cue.load_ogf_bridge", return_value=None), \
                patch.object(stdio_main, "ensure_owner_storage"), \
                patch.object(stdio_main, "owner_journal_path", return_value=ROOT / "synthetic-journal.json"), \
                patch("sora_commander_next.jobs.JobManager") as managers, \
                patch.object(windows_owner_controller, "WindowsOwnerController",
                             side_effect=lambda host: SimpleNamespace(host=host, _verify=Mock())), \
                patch.object(owner_review_dispatch, "OwnerReviewDispatcher", Dispatcher):
            guard_type.return_value.sid = "synthetic-owner"
            host = bridge.build_exec_host(ROOT)
            guard_type.return_value.verify.assert_called_once()
            self.assertEqual(host.owner, "synthetic-owner")
            self.assertEqual(host.policy.roots, (str(ROOT / "src"),))
            self.assertEqual(host.policy.read_only_roots, (str(ROOT / "docs"),))
            self.assertIsNone(getattr(host, "_edit_review_pilot", None))
            self.assertEqual(host.task_presets, ("preset",))
            managers.assert_called_once_with(journal_path=ROOT / "synthetic-journal.json",
                output_archive_path=ROOT / "owner-output-archive.json", protected_stage=True)
            self.assertIs(type(host._owner_review_dispatcher), Dispatcher)
            with patch.object(Host, "attach_owner_review_local", side_effect=RuntimeError("synthetic attach failure")), \
                    self.assertRaises(RuntimeError):
                bridge.build_exec_host(ROOT)
            Dispatcher.fail = True
            with self.assertRaises(RuntimeError):
                bridge.build_exec_host(ROOT)

    def test_missing_dispatcher_is_refused(self):
        with patch.dict(os.environ, {"SORA_NEXT_PRODUCT_BROKER": "0"}), \
                patch("sora_commander_next.stdio_main.build_owner_pilot_host",
                      return_value=SimpleNamespace(_owner_review_dispatcher=None)):
            with self.assertRaises(RuntimeError):
                bridge.build_exec_host(ROOT)

    def test_conflicting_optins_are_refused(self):
        with patch.dict(os.environ, {"SORA_NEXT_PRODUCT_BROKER": "1"}), self.assertRaises(ValueError):
            bridge.build_exec_host(ROOT)
        with patch.dict(os.environ, {"SORA_NEXT_PRODUCT_BROKER": "0"}), self.assertRaises(ValueError):
            bridge.build_exec_host(ROOT, enable_create=True)


@unittest.skipUnless(os.name == "nt", "Windows dispatcher module")
class ReviewContractTests(unittest.TestCase):
    def test_fake_dispatcher_preserves_approve_reject_closed_and_expired(self):
        # Exercise Host admission and observation. Synthetic decision delivery is
        # not evidence for real Windows UI, nonce verification or process spawn.
        for outcome in ("RUNNING", "DENIED", "EXPIRED", "CLOSED"):
            jobs = Mock()
            jobs._stdin_pending = {}
            jobs.start.return_value = {"status": "RUNNING", "session_id": "a" * 48}
            jobs.wait_for.return_value = {"status": "EXITED", "session_id": "a" * 48, "exit_code": 0}
            host = Host(policy=Policy(roots=(str(ROOT),)), owner="synthetic-owner", jobs=jobs)
            result = {}

            class Dispatcher:
                def __init__(self):
                    self._controller = SimpleNamespace(host=host, _verify=Mock())

                def offer_local(self, pending_id):
                    if outcome == "RUNNING":
                        result.update(jobs.start("synthetic approved request"))
                    elif outcome == "DENIED":
                        result.update(status="DENIED", reason="DENIED", launch_outcome="NOT_STARTED")
                    else:
                        result.update(status="TIMED_OUT", reason=outcome, stage="FIRST_GUI",
                            approval_state="EXPIRED", launch_outcome="NOT_STARTED",
                            required_action="REPORT_AND_WAIT", auto_retry=False)
                    return True

                def review_status(self, pending_id):
                    return dict(result)

            with patch("sora_commander_next.owner_review_dispatch.OwnerReviewDispatcher", Dispatcher):
                host.attach_owner_review_local(Dispatcher())
            reply = host.run_command_wait(executable=sys.executable, argv=["--version"],
                                          cwd=str(ROOT), wait_seconds=0)
            if outcome == "RUNNING":
                self.assertEqual(reply["status"], "EXITED")
                jobs.start.assert_called_once()
            else:
                self.assertEqual(reply["status"], "DENIED" if outcome == "DENIED" else "TIMED_OUT")
                self.assertEqual(reply["launch_outcome"], "NOT_STARTED")
                jobs.start.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows synthetic read fixture")
class ReadAndWriteGateTests(unittest.TestCase):
    def test_readonly_file_read_still_uses_union_of_roots(self):
        from tests._mock_read_boundary import canonical_fixture_admission
        from sora_commander_next.file_reads import read_text_file
        with inherited_test_temp() as folder, canonical_fixture_admission():
            root = Path(folder)
            file = root / "example.txt"
            file.write_text("synthetic bridge read", encoding="utf-8")
            host = Host(policy=Policy(roots=(), read_only_roots=(str(root),)))
            result = read_text_file(str(file), host.policy.read_roots)
            self.assertIn("synthetic bridge read", str(result))

    def test_exec_dev_write_gate_refuses_before_mutation(self):
        import json
        from sora_commander_next.mcp_adapter import build_server
        host = Host(policy=Policy(roots=(str(ROOT),)))
        with patch("sora_commander_next.dev_rdc_compat.mutate") as mutate:
            server = build_server(host, dev_writes_enabled=False, dev_write_tools_enabled=True)
            response = asyncio.run(server.call_tool("create_file", {
                "path": str(ROOT / "synthetic-never-created.txt"), "content": "synthetic"}))
        content = response[0] if isinstance(response, tuple) else response
        payload = json.loads(next(item.text for item in content if getattr(item, "type", None) == "text"))
        self.assertEqual(payload["status"], "DENY")
        self.assertEqual(payload["error"], "DevWritesDisabled")
        mutate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
