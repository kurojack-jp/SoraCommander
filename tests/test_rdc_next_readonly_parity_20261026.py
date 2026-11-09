"""Offline SDK visibility diagnostic; mocked host/readers, no runtime startup.

CHAT_NAMES is the Owner-supplied 2026-10-26 Chat observation, not a live probe.
"""
import asyncio
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from sora_commander_next import mcp_adapter as adapter
from mcp.server.fastmcp.exceptions import ToolError

CHAT_NAMES = frozenset("run_command ping status review_status read_process_output "
    "list_sessions previous_sessions stop_process preview_text_edit request_text_edit "
    "edit_review_status request_text_create list_folder read_text_file".split())
CASES = {
    "read_many_text_files": {"paths": ["fixture.py"]},
    "search_code": {"path": "fixture", "text": "needle"},
    "find_files": {"path": "fixture", "name": "file"},
    "read_text_page": {"path": "fixture.py"},
    "read_text_chunk": {"path": "fixture.py"},
    "sha256_file": {"path": "fixture.py"},
    "path_info": {"path": "fixture.py"},
}


class ReadonlyParityTests(unittest.TestCase):
    def server(self, roots=("MOCK_CONFIGURED_ROOT",), pilot=False):
        host = SimpleNamespace(policy=SimpleNamespace(roots=roots, read_roots=roots))
        if pilot:
            host._edit_review_pilot = SimpleNamespace(enable_create=True)
        return adapter.build_server(host), host

    def test_sdk_list_tools_and_owner_chat_gap(self):
        for pilot, count in ((False, 31), (True, 37)):
            with self.subTest(pilot=pilot):
                server, host = self.server(pilot=pilot)
                definitions = asyncio.run(server.list_tools())
                names = {tool.name for tool in definitions}
                catalog = adapter._local_registered_catalog(server, host)
                self.assertEqual(len(names), count)
                self.assertEqual(set(catalog["registered_tool_names"]), names)
                self.assertEqual(catalog["registered_tool_count"], count)
                self.assertEqual(catalog["status"], "LOCAL_REGISTRATION_SNAPSHOT")
                self.assertEqual(catalog["chatgpt_tool_visibility"], "UNVERIFIED")
                self.assertEqual(catalog["protected_broker_acceptance"],
                                 "NOT_ESTABLISHED_BY_CATALOG")
                self.assertEqual(len(catalog["tool_schema_sha256"]), 64)
                self.assertTrue(set(CASES) <= names - CHAT_NAMES)
                if pilot:
                    self.assertTrue(CHAT_NAMES <= names)
                    print("LOCAL_SDK=37 OWNER_CHAT=14 LOCAL_ONLY=" +
                          ",".join(sorted(names - CHAT_NAMES)))

    def test_readers_receive_same_configured_roots(self):
        server, host = self.server()
        for name, args in CASES.items():
            with self.subTest(name=name), patch.object(
                    adapter, name + "_readonly", return_value={"mock": True}) as reader:
                asyncio.run(server.call_tool(name, args))
                reader.assert_called_once()
                self.assertIs(reader.call_args.args[1], host.policy.roots)

    def test_empty_roots_refuse_before_reader(self):
        server, _ = self.server(roots=())
        for name, args in CASES.items():
            with self.subTest(name=name), patch.object(adapter, name + "_readonly") as reader:
                with self.assertRaises(ToolError):
                    asyncio.run(server.call_tool(name, args))
                reader.assert_not_called()

    def test_invalid_wire_types_and_limits_never_reach_reader(self):
        server, _ = self.server()
        invalid = [
            ("read_many_text_files", {"paths": "x"}),
            ("read_many_text_files", {"paths": []}),
            ("read_many_text_files", {"paths": [str(i) for i in range(9)]}),
            ("read_many_text_files", {"paths": ["x", "x"]}),
            ("read_many_text_files", {"paths": ["x"], "max_bytes_per_file": True}),
            ("read_many_text_files", {"paths": ["x"], "max_bytes_per_file": 8193}),
            ("search_code", {"path": "x", "text": "x\ny"}),
            ("search_code", {"path": "x", "text": "x", "depth": 13}),
            ("search_code", {"path": "x", "text": "x", "max_results": True}),
            ("read_text_page", {"path": "x", "offset": True}),
            ("read_text_page", {"path": "x", "max_bytes": 65537}),
            ("read_text_chunk", {"path": "x", "offset": -2}),
            ("read_text_chunk", {"path": "x", "max_bytes": 3}),
            ("sha256_file", {"path": 1}),
            ("sha256_file", {"path": "x", "roots": ["injected"]}),
        ]
        invalid += [(name, {**args, "paths": ["x\0y"]} if name == "read_many_text_files"
                     else {**args, "path": "x\0y"}) for name, args in CASES.items()]
        for name, args in invalid:
            with self.subTest(name=name, args=args), patch.object(
                    adapter, name + "_readonly") as reader:
                with self.assertRaises(ToolError):
                    asyncio.run(server.call_tool(name, args))
                reader.assert_not_called()

    def test_mocked_reader_errors_are_sanitized(self):
        server, _ = self.server()
        for name, args in CASES.items():
            with self.subTest(name=name), patch.object(adapter, name + "_readonly",
                    side_effect=PermissionError("PRIVATE_PATH_SENTINEL")):
                with self.assertRaises(ToolError) as caught:
                    asyncio.run(server.call_tool(name, args))
                self.assertNotIn("PRIVATE_PATH_SENTINEL", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
