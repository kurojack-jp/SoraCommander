"""Real pagination IO behind an explicit mock boundary; no policy acceptance."""

import asyncio
from hashlib import sha256
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import mock_read_boundary
import unittest

from sora_commander_next.file_reads import (
    MAX_PAGED_FILE_BYTES, ReadBoundaryError, read_text_file, read_text_page,
)
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import _validate_arguments, build_server


class ReadTextPageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(inherited_test_temp())).resolve()
        self.path = self.root / "long-code.py"
        self.enterContext(mock_read_boundary(self.root, (self.path.name,)))

    def read(self, **kwargs):
        return read_text_page(str(self.path), (str(self.root),), **kwargs)

    def test_large_utf8_code_roundtrip_pages_and_whole_file_digest(self):
        raw = (("print('日本語')\r\n" * 7000) + "last line\n").encode("utf-8")
        self.assertGreater(len(raw), 65536)
        self.path.write_bytes(raw)
        with self.assertRaises(ReadBoundaryError):
            read_text_file(str(self.path), (str(self.root),))
        offset, chunks = 0, []
        for _ in range(100):
            row = self.read(offset=offset, max_bytes=4096)
            self.assertEqual(row["offset"], offset)
            self.assertEqual(row["total_bytes"], len(raw))
            self.assertEqual(row["sha256"], sha256(raw).hexdigest())
            self.assertLessEqual(row["next_offset"] - offset, 4096)
            chunks.append(row["content"])
            offset = row["next_offset"]
            if not row["truncated"]:
                break
        self.assertEqual(offset, len(raw))
        self.assertEqual("".join(chunks).encode("utf-8"), raw)

    def test_utf8_boundary_and_mid_character_offset(self):
        self.path.write_bytes("あいう😀end".encode("utf-8"))
        a = self.read(max_bytes=5)
        self.assertEqual(a["content"], "あ")
        self.assertEqual(a["next_offset"], 3)
        b = self.read(offset=3, max_bytes=6)
        self.assertEqual(b["content"], "いう")
        self.assertTrue(b["truncated"])
        with self.assertRaises(ReadBoundaryError):
            self.read(offset=1, max_bytes=8)
        with self.assertRaises(ReadBoundaryError):
            self.read(max_bytes=1)

    def test_empty_file_and_end_offset(self):
        self.path.write_bytes(b"")
        result = self.read()
        self.assertEqual(result["content"], "")
        self.assertEqual(result["next_offset"], 0)
        self.assertFalse(result["truncated"])
        self.path.write_bytes(b"abc")
        self.assertEqual(self.read(offset=3)["content"], "")
        self.assertFalse(self.read(offset=3)["truncated"])
        with self.assertRaises(ReadBoundaryError):
            self.read(offset=4)

    def test_bounds_invalid_types_and_file_limit(self):
        self.path.write_bytes(b"12345")
        for offset in (True, 1.5, "0", -1, MAX_PAGED_FILE_BYTES + 1):
            with self.subTest(offset=offset), self.assertRaises(ReadBoundaryError):
                self.read(offset=offset)
        for max_bytes in (True, None, "10", 0, 65537):
            with self.subTest(max_bytes=max_bytes), self.assertRaises(ReadBoundaryError):
                self.read(max_bytes=max_bytes)
        self.path.write_bytes(b"a" * (MAX_PAGED_FILE_BYTES + 1))
        with self.assertRaises(ReadBoundaryError):
            self.read()

    def test_binary_and_unregistered_mock_paths_rejected(self):
        self.path.write_bytes(b"\xffbad")
        with self.assertRaises(ReadBoundaryError):
            self.read()
        # Abstract boundary rejection only. Production path/link checks are
        # independently exercised in test_scn16_option_b_read_boundary_20261102.
        for foreign in (self.root.parent / "outside.txt", self.root / "link.txt"):
            with self.assertRaises(ReadBoundaryError):
                read_text_page(str(foreign), (str(self.root),))

    def test_mcp_tool_pages_large_file_and_rejects_bad_input(self):
        self.path.write_text("abc日本語" * 16000, encoding="utf-8")
        host = Host(policy=Policy(roots=(str(self.root),)))
        try:
            server = build_server(host)
            result = asyncio.run(server.call_tool(
                "read_text_page", {"path": str(self.path), "max_bytes": 2048}))
            self.assertIn("next_offset", str(result))
            self.assertIn("truncated", str(result))
            for bad in ({"offset": True}, {"offset": -1},
                        {"max_bytes": "1000"}, {"offset": 5.5}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    _validate_arguments("read_text_page", {"path": str(self.path), **bad})
        finally:
            host.jobs.close()

    def test_mcp_denies_default_empty_roots(self):
        host = Host()
        try:
            from mcp.server.fastmcp.exceptions import ToolError
            server = build_server(host)
            with self.assertRaises(ToolError):
                asyncio.run(server.call_tool(
                    "read_text_page", {"path": str(self.path)}))
        finally:
            host.jobs.close()


if __name__ == "__main__":
    unittest.main()
