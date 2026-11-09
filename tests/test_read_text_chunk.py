"""Real tail/cursor IO behind a mock boundary; no path-policy acceptance."""

import asyncio
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import mock_read_boundary
import unittest
from unittest.mock import patch

from sora_commander_next.file_reads import ReadBoundaryError, read_text_chunk
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import _validate_arguments, build_server


class TextChunkTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(inherited_test_temp())).resolve()
        self.log = self.root / "build.log"
        self.enterContext(mock_read_boundary(self.root, (self.log.name,)))

    def chunk(self, **kwargs):
        return read_text_chunk(str(self.log), (str(self.root),), **kwargs)

    def test_read_tail_of_5mb_log_without_full_file_read(self):
        suffix = "\n日本語 build finished\n".encode("utf-8")
        self.log.write_bytes(b"." * (5 * 1024 * 1024) + suffix)
        result = self.chunk(max_bytes=48)
        self.assertTrue(result["content"].endswith("日本語 build finished\n"))
        self.assertGreater(result["start_offset"], 4 * 1024 * 1024)
        self.assertEqual(result["next_offset"], result["total_bytes"])
        self.assertFalse(result["truncated"])
        self.assertTrue(result["prefix_skipped"])

    def test_follow_appended_log_using_byte_cursor(self):
        self.log.write_bytes("start\n".encode())
        initial = self.chunk()
        self.assertEqual(initial["content"], "start\n")
        with self.log.open("ab") as f:
            f.write("追加ログ😀\n".encode("utf-8"))
        later = self.chunk(offset=initial["next_offset"])
        self.assertEqual(later["content"], "追加ログ😀\n")
        self.assertEqual(later["start_offset"], initial["next_offset"])
        again = self.chunk(offset=later["next_offset"])
        self.assertEqual(again["content"], "")
        self.assertFalse(again["truncated"])

    def test_utf8_partial_boundary_rolls_cursor_to_valid_byte(self):
        self.log.write_bytes(b"head-" + "日本語".encode("utf-8") + b"-end\n")
        first = self.chunk(offset=0, max_bytes=6)
        self.assertEqual(first["content"], "head-")
        self.assertEqual(first["next_offset"], 5)
        second = self.chunk(offset=5, max_bytes=4)
        self.assertEqual(second["content"], "日")
        self.assertEqual(second["next_offset"], 8)
        with self.assertRaises(ReadBoundaryError):
            self.chunk(offset=6, max_bytes=8)

    def test_tail_resynchronizes_utf8_start(self):
        self.log.write_bytes(b"X" * 40 + "日本語END\n".encode("utf-8"))
        result = self.chunk(offset=-1, max_bytes=10)
        self.assertEqual(result["content"], "\u672c\u8a9eEND\n")
        self.assertGreater(result["start_offset"], 40)
        self.assertEqual(result["next_offset"], result["total_bytes"])

    def test_invalid_limits_binary_and_scope_denied(self):
        self.log.write_bytes(b"plain")
        for bad in (True, 1.5, "0", -2, 256 * 1024 * 1024 + 1):
            with self.subTest(offset=bad), self.assertRaises(ReadBoundaryError):
                self.chunk(offset=bad)
        for bad in (True, None, 3, 65537, "16"):
            with self.subTest(max_bytes=bad), self.assertRaises(ReadBoundaryError):
                self.chunk(max_bytes=bad)
        with self.assertRaises(ReadBoundaryError):
            self.chunk(offset=6)
        self.log.write_bytes(b"\xff\xff\xff\xff")
        with self.assertRaises(ReadBoundaryError):
            self.chunk(offset=0)
        # No filesystem access: this is the abstract fixture's scope contract.
        with self.assertRaises(ReadBoundaryError):
            read_text_chunk(str(self.root.parent / "outside.log"), (str(self.root),))

    def test_unregistered_mock_alias_and_real_size_limit_refused(self):
        self.log.write_text("hello", encoding="utf-8")
        alias = self.root / "alias.log"
        # Production link policy has a separate mocked-metadata test. Do not
        # create privileged links or imply this registry proves link safety.
        with self.assertRaises(ReadBoundaryError):
            read_text_chunk(str(alias), (str(self.root),))
        with patch("sora_commander_next.file_reads.MAX_STREAM_FILE_BYTES", 4):
            with self.assertRaises(ReadBoundaryError):
                self.chunk()

    def test_live_mcp_sdk_has_tool_and_sanitizes_bounds(self):
        self.log.write_text("line one\n続きの行\n", encoding="utf-8")
        host = Host(policy=Policy(roots=(str(self.root),)))
        try:
            server = build_server(host)
            self.assertIn("read_text_chunk",
                          {t.name for t in server._tool_manager.list_tools()})
            _, result = asyncio.run(server.call_tool(
                "read_text_chunk", {"path": str(self.log), "offset": 0}))
            self.assertEqual(result["content"], self.log.read_bytes().decode("utf-8"))
            for bad in ({"offset": True}, {"offset": -2},
                        {"max_bytes": 3}, {"max_bytes": "12"},
                        {"path": "\0broken"}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    _validate_arguments("read_text_chunk",
                                        {"path": str(self.log), **bad})
        finally:
            host.jobs.close()

    def test_default_host_does_not_open_log(self):
        from mcp.server.fastmcp.exceptions import ToolError
        self.log.write_text("private log", encoding="utf-8")
        host = Host()
        try:
            server = build_server(host)
            with self.assertRaises(ToolError):
                asyncio.run(server.call_tool(
                    "read_text_chunk", {"path": str(self.log)}))
        finally:
            host.jobs.close()


if __name__ == "__main__":
    unittest.main()
