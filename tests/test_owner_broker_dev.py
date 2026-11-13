"""The Host-and-Broker split offers the same tools as today, in memory.

No pipe, Owner window, job or real file write. Writers are mocked in the backend.
"""
import asyncio
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from sora_commander_next.broker_host import BrokerHost, UnavailableTransport
from sora_commander_next.edit_review_pilot import EditReviewPilot
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import _local_registered_catalog, build_server
from sora_commander_next.owner_broker_dev import DevBrokerHost, OwnerBrokerBackend
from sora_commander_next.owner_broker_service import OwnerBrokerService, _SCHEMAS
from sora_commander_next.file_edits import StalePreviewError
from sora_commander_next.file_reads import _sensitive_component
from sora_commander_next.policy import Policy

ROOT = Path(__file__).resolve().parents[1]
# The read boundary refuses a path with a protected name, such as Temp, by design.
CHECKOUT_READABLE = not any(_sensitive_component(part) for part in ROOT.parts[1:])
PILOT_ROOT = ROOT / "tests" / "real_mcp"
SHA = "a" * 64
RID = "1" * 32


class Transport:
    def __init__(self, service):
        self.service = service
        self.frames = []

    def request(self, raw):
        self.frames.append(raw)
        return self.service.handle(raw)

    def close(self):
        pass


def catalog(host):
    server = build_server(host)
    return _local_registered_catalog(server, host), server


def tools(server):
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def call(server, name, arguments):
    return asyncio.run(server.call_tool(name, arguments))[1]


def legacy_host():
    host = Host(policy=Policy(roots=(str(ROOT),)))
    host._edit_review_pilot = EditReviewPilot(PILOT_ROOT, enable_create=True)
    return host


class FakePilot:
    enable_create = True

    def __init__(self):
        self.calls = []
        self.error = None

    def request(self, path, content, expected_sha256):
        self.calls.append(("request", path, content, expected_sha256))
        if self.error is not None:
            raise self.error
        return {"status": "REVIEW", "pending_id": "p" * 32}

    def request_create(self, path, content):
        self.calls.append(("create", path, content))
        if self.error is not None:
            raise self.error
        return {"status": "REVIEW", "pending_id": "q" * 32}

    def status(self, pending_id):
        self.calls.append(("status", pending_id))
        return {"status": "COMMITTED", "pending_id": pending_id}


class ClientScopeTests(unittest.TestCase):
    def test_outside_full_scope_writes_are_refused_before_transport(self):
        transport = Mock()
        client = DevBrokerHost(transport, read_roots=(str(ROOT),),
                               edit_pilot=True, create_pilot=True)
        # Lexical synthetic paths only; no files or folders are created.
        outside = str(ROOT.parent / "outside-broker-synthetic" / "note.txt")
        inside = str(ROOT / "tests" / "broker-note.txt")
        cases = (("create_directory", {"path": outside}),
                 ("create_file", {"path": outside, "content": "new"}),
                 ("move_file", {"source": outside, "destination": inside}),
                 ("move_file", {"source": inside, "destination": outside}),
                 ("request_source_patch_dev", {"path": outside, "old_text": "old",
                                               "new_text": "new", "expected_sha256": SHA}),
                 ("request_text_edit", {"path": outside, "content": "new", "expected_sha256": SHA}),
                 ("request_text_create", {"path": outside, "content": "new"}))
        for action, arguments in cases:
            with self.subTest(action=action, arguments=tuple(arguments)):
                if action == "request_text_edit":
                    result = client._edit_review_pilot.request(**arguments)
                elif action == "request_text_create":
                    result = client._edit_review_pilot.request_create(**arguments)
                else:
                    result = client.dev_mutate(action, **arguments)
                self.assertEqual(result, {"status": "DENY", "error": "FullScopeRequired"})
                transport.request.assert_not_called()


class ToolParityTests(unittest.TestCase):
    def setUp(self):
        self.backend_host = legacy_host()
        self.addCleanup(self.backend_host.close)
        self.pilot = FakePilot()
        self.backend_host._edit_review_pilot = self.pilot
        self.transport = Transport(OwnerBrokerService(OwnerBrokerBackend(self.backend_host)))
        self.client = DevBrokerHost(self.transport, read_roots=(str(ROOT),),
                                    edit_pilot=True, create_pilot=True)
        self.server = build_server(self.client)

    def test_tool_names_and_schemas_match_todays_host(self):
        reference = legacy_host()
        self.addCleanup(reference.close)
        expected, _ = catalog(reference)
        actual, _ = catalog(self.client)
        self.assertEqual(actual["registered_tool_count"], 54)
        self.assertEqual(actual["tool_schema_sha256"], expected["tool_schema_sha256"])

    def test_descriptions_match_todays_host(self):
        reference = legacy_host()
        self.addCleanup(reference.close)
        expected = tools(build_server(reference))
        actual = tools(self.server)
        self.assertEqual(set(actual), set(expected))
        for name in expected:
            with self.subTest(name=name):
                self.assertEqual(actual[name].description, expected[name].description)

    def test_plain_broker_host_gets_no_dev_tools(self):
        names = set(tools(build_server(BrokerHost(UnavailableTransport()))))
        self.assertFalse(names & {"create_file", "move_file", "send_input",
                                  "request_source_patch_dev"})

    def test_a_plain_broker_host_cannot_claim_the_dev_role(self):
        host = BrokerHost(UnavailableTransport())
        host.dev_broker_client = True
        self.assertNotIn("create_file", tools(build_server(host)))

    def test_writers_are_absent_from_the_client(self):
        for name in ("jobs", "approval", "_owner_review_dispatcher", "worker"):
            self.assertFalse(hasattr(self.client, name), name)

    def test_dev_writers_reach_the_broker_only(self):
        with patch("sora_commander_next.dev_rdc_compat.mutate",
                   return_value={"status": "OK"}) as mutate:
            cases = (("create_directory", {"path": str(ROOT / "tests" / "broker-new")}),
                     ("create_file", {"path": str(ROOT / "tests" / "broker-new.txt"), "content": "日本語"}),
                     ("move_file", {"source": str(ROOT / "tests" / "broker-a.txt"),
                                    "destination": str(ROOT / "tests" / "broker-b.txt")}),
                     ("request_source_patch_dev", {"path": str(ROOT / "tests" / "broker-a.py"), "old_text": "a",
                                                   "new_text": "b", "expected_sha256": SHA}))
            for name, arguments in cases:
                with self.subTest(name=name):
                    mutate.reset_mock()
                    self.assertEqual(call(self.server, name, arguments)["status"], "OK")
                    mutate.assert_called_once_with(name, (str(ROOT),), **arguments)

    def test_large_japanese_file_fits_the_request_limit(self):
        content = "あ" * 21000  # 63 KB of UTF-8, six bytes each once escaped.
        path = str(ROOT / "tests" / "broker-big.txt")
        with patch("sora_commander_next.dev_rdc_compat.mutate",
                   return_value={"status": "OK"}) as mutate:
            result = call(self.server, "create_file", {"path": path, "content": content})
        self.assertEqual(result["status"], "OK")
        mutate.assert_called_once_with("create_file", (str(ROOT),), path=path, content=content)

    def test_edit_tools_reach_the_brokers_pilot(self):
        edit_path = str(ROOT / "tests" / "broker-a.txt")
        create_path = str(ROOT / "tests" / "broker-b.txt")
        result = call(self.server, "request_text_edit", {
            "path": edit_path, "content": "new", "expected_sha256": SHA})
        self.assertEqual(result["pending_id"], "p" * 32)
        self.assertEqual(self.pilot.calls[-1], ("request", edit_path, "new", SHA))
        result = call(self.server, "request_text_create", {"path": create_path, "content": "c"})
        self.assertEqual(result["pending_id"], "q" * 32)
        self.assertEqual(self.pilot.calls[-1], ("create", create_path, "c"))
        result = call(self.server, "edit_review_status", {"pending_id": "p" * 32})
        self.assertEqual(result["status"], "COMMITTED")

    def test_edit_wait_tool_observes_through_the_broker_without_resubmitting(self):
        path = str(ROOT / "tests" / "broker-a.txt")
        result = call(self.server, "request_text_edit_wait", {
            "path": path, "content": "new", "expected_sha256": SHA,
            "wait_seconds": 1.0})
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual([c for c in self.pilot.calls if c[0] == "request"],
                         [("request", path, "new", SHA)])

    def test_edit_failures_give_the_same_fixed_results_as_today(self):
        path = str(ROOT / "tests" / "broker-a.txt")
        self.pilot.error = StalePreviewError("secret path")
        result = call(self.server, "request_text_edit", {
            "path": path, "content": "n", "expected_sha256": SHA})
        self.assertEqual(result, {"status": "ERROR", "error": "STALE_SHA256"})
        self.pilot.error = RuntimeError("secret path")
        result = call(self.server, "request_text_edit", {
            "path": path, "content": "n", "expected_sha256": SHA})
        self.assertEqual(result, {"status": "ERROR", "error": "EDIT_REJECTED"})
        result = call(self.server, "request_text_create", {"path": path, "content": "n"})
        self.assertEqual(result, {"status": "ERROR", "error": "CREATE_REJECTED"})

    def test_interactive_is_denied_with_no_frame_sent(self):
        before = len(self.transport.frames)
        result = call(self.server, "run_command", {
            "executable": r"C:\x.exe", "argv": [], "cwd": str(ROOT), "interactive": True,
            "background": True})
        self.assertEqual(result["error"], "DevInteractiveOnly")
        result = call(self.server, "send_input", {
            "session_id": "a" * 48, "input_text": "1\n", "input_id": "b" * 32})
        self.assertEqual(result["error"], "DevInteractiveOnly")
        self.assertEqual(len(self.transport.frames), before)

    @unittest.skipUnless(CHECKOUT_READABLE, "checkout path has a protected name")
    def test_read_tool_stays_in_the_host_process_with_no_frame(self):
        before = len(self.transport.frames)
        result = call(self.server, "path_info", {"path": str(ROOT / "pyproject.toml")})
        self.assertNotIn("error", {k: v for k, v in result.items() if v is None})
        self.assertEqual(len(self.transport.frames), before)

    def test_recovery_and_status_come_from_the_broker(self):
        before = len(self.transport.frames)
        call(self.server, "recovery_overview", {})
        status = call(self.server, "status", {})
        self.assertEqual(len(self.transport.frames), before + 2)
        self.assertIs(status["remote_ready"], False)
        self.assertEqual(status["host_role"], "DEV_BROKER_CLIENT")
        self.assertEqual(status["approval"], "LOCAL_DEMO_ONLY")

    @unittest.skipUnless(CHECKOUT_READABLE, "checkout path has a protected name")
    def test_absent_broker_keeps_reads_and_refuses_the_rest(self):
        client = DevBrokerHost(UnavailableTransport(), read_roots=(str(ROOT),),
                               edit_pilot=True, create_pilot=True)
        server = build_server(client)
        self.assertEqual(call(server, "ping", {})["status"], "OK")
        self.assertEqual(call(server, "status", {})["status"], "UNAVAILABLE")
        self.assertIs(call(server, "status", {})["remote_ready"], False)
        self.assertEqual(call(server, "create_file", {
            "path": str(ROOT / "tests" / "broker-absent.txt"), "content": "c"})["status"],
                         "UNAVAILABLE")
        self.assertEqual(call(server, "run_command_wait", {
            "executable": r"C:\x.exe", "argv": [], "cwd": str(ROOT)})["status"], "UNAVAILABLE")
        self.assertNotIn("error", call(server, "path_info", {"path": str(ROOT / "pyproject.toml")}))

    def test_unknown_operation_cannot_be_smuggled_through_dev_mutate(self):
        self.assertEqual(self.client.dev_mutate("close")["error"], "ActionNotAllowed")
        self.assertEqual(self.client.dev_mutate("_mutate")["error"], "ActionNotAllowed")

    def test_backend_exposes_exactly_the_allowed_actions(self):
        backend = OwnerBrokerBackend(self.backend_host)
        for action in _SCHEMAS:
            self.assertTrue(callable(getattr(backend, action)), action)
        for name in ("jobs", "approval", "close", "attach_owner_review_local", "policy"):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                getattr(backend, name)

    def test_create_pilot_requires_the_edit_pilot(self):
        with self.assertRaises(ValueError):
            DevBrokerHost(UnavailableTransport(), create_pilot=True)


if __name__ == "__main__":
    unittest.main()
