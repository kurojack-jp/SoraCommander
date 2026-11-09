"""Streamed read-only SHA-256 for large binaries in configured roots."""

import asyncio
from hashlib import sha256
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
import unittest
from unittest.mock import patch

from sora_commander_next.file_reads import (
    ReadBoundaryError, sha256_file, path_info,
)
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import _validate_arguments, build_server


class Sha256FileTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
        self.root=Path(self.enterContext(inherited_test_temp())).resolve()
        self.file=self.root/"release.bin"

    def hash(self):
        return sha256_file(str(self.file),(str(self.root),))

    def test_large_binary_digest_and_byte_count(self):
        contents=(bytes(range(256)) * 24000) + b"release trailer"
        self.file.write_bytes(contents)
        self.assertIsNone(path_info(str(self.file),(str(self.root),))["sha256"])
        row=self.hash()
        self.assertEqual(row["sha256"],sha256(contents).hexdigest())
        self.assertEqual(row["size_bytes"],len(contents))
        self.assertIs(type(row["modified_ns"]),int)

    def test_empty_binary(self):
        self.file.write_bytes(b"")
        row=self.hash()
        self.assertEqual(row["sha256"],sha256(b"").hexdigest())
        self.assertEqual(row["size_bytes"],0)

    def test_directory_out_of_scope_and_links_denied(self):
        self.file.write_bytes(b"abc")
        with self.assertRaises(ReadBoundaryError):
            sha256_file(str(self.root),(str(self.root),))
        with inherited_test_temp() as out:
            secret=Path(out)/"private.bin"
            secret.write_bytes(b"secret")
            with self.assertRaises(ReadBoundaryError):
                sha256_file(str(secret),(str(self.root),))
            link=self.root/"linked.bin"
            try:
                link.symlink_to(secret)
                # The inherited fixture refuses links; remove ours first.
                self.addCleanup(link.unlink)
            except (OSError,NotImplementedError):
                pass
            else:
                with self.assertRaises(ReadBoundaryError):
                    sha256_file(str(link),(str(self.root),))

    def test_bounded_read_limit_denied(self):
        self.file.write_bytes(b"X"*32)
        with patch("sora_commander_next.file_reads.MAX_HASH_FILE_BYTES",16):
            with self.assertRaises(ReadBoundaryError):
                self.hash()

    def test_sdk_end_to_end_and_strict_wire(self):
        self.file.write_bytes(b"\x00\xffbinary")
        host=Host(policy=Policy(roots=(str(self.root),)))
        try:
            server=build_server(host)
            self.assertIn("sha256_file",
                          {tool.name for tool in server._tool_manager.list_tools()})
            _,row=asyncio.run(server.call_tool(
                "sha256_file",{"path":str(self.file)}))
            self.assertEqual(row["sha256"],sha256(self.file.read_bytes()).hexdigest())
            self.assertNotIn("content",row)
            for args in ({"path":"\0evil"},{"path":42},
                         {"path":str(self.file),"allow_write":True}):
                with self.subTest(args=args),self.assertRaises(ValueError):
                    _validate_arguments("sha256_file",args)
        finally:
            host.jobs.close()

    def test_default_host_does_not_hash_private_paths(self):
        from mcp.server.fastmcp.exceptions import ToolError
        self.file.write_bytes(b"abc")
        host=Host()
        try:
            server=build_server(host)
            with self.assertRaises(ToolError):
                asyncio.run(server.call_tool(
                    "sha256_file",{"path":str(self.file)}))
        finally:
            host.jobs.close()


if __name__=="__main__":
    unittest.main()
