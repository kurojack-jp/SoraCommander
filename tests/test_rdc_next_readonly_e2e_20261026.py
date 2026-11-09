"""Real local SDK read-only parity acceptance with disposable files.

No repository traversal, network service, remote tool registration, or writes
outside an owned inherited_test_temp fixture. No protected Owner authority.
"""
import asyncio
from hashlib import sha256
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from types import SimpleNamespace
import unittest

from mcp.server.fastmcp.exceptions import ToolError
from sora_commander_next.mcp_adapter import build_server

ALLOWED = (
    "list_folder_page", "read_text_page", "find_files", "search_code",
    "path_info", "read_text_lines", "read_many_text_files",
    "compare_text_files", "read_text_chunk", "sha256_file",
)


class LocalReadonlyE2E(unittest.TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
        self.temp = self.enterContext(inherited_test_temp())
        self.root = Path(self.temp)
        self.original = self.root / "alpha.py"
        self.modified = self.root / "beta.py"
        self.original.write_bytes(b"first = 1\nHELLO_SYMBOL = 'old'\n")
        self.modified.write_bytes(b"first = 2\nHELLO_SYMBOL = 'new'\n")
        roots = (str(self.root),)
        self.host = SimpleNamespace(policy=SimpleNamespace(roots=roots, read_roots=roots))
        self.server = build_server(self.host)

    def call(self, name, arguments):
        self.assertIn(name, ALLOWED)
        return asyncio.run(self.server.call_tool(name, arguments))

    def test_real_registration_contains_all_selected_read_only_tools(self):
        names = {tool.name for tool in asyncio.run(self.server.list_tools())}
        self.assertTrue(set(ALLOWED).issubset(names))

    def test_real_batch_read_without_rdc(self):
        result = self.call("read_many_text_files", {
            "paths": [str(self.original), str(self.modified)]
        })
        self.assertIn("HELLO_SYMBOL", str(result))
        self.assertIn(sha256(self.original.read_bytes()).hexdigest(), str(result))

    def test_real_bounded_name_search(self):
        result = self.call("find_files", {
            "path": str(self.root), "name": "alpha"
        })
        self.assertIn("alpha.py", str(result))

    def test_real_literal_source_search(self):
        result = self.call("search_code", {
            "path": str(self.root), "text": "HELLO_SYMBOL"
        })
        self.assertIn("alpha.py", str(result))
        self.assertIn("beta.py", str(result))

    def test_real_page_read(self):
        result = self.call("read_text_page", {
            "path": str(self.original), "offset": 0, "max_bytes": 24
        })
        self.assertIn("first", str(result))

    def test_real_numbered_lines(self):
        result = self.call("read_text_lines", {
            "path": str(self.original), "start_line": 2, "max_lines": 1
        })
        self.assertIn("HELLO_SYMBOL", str(result))

    def test_real_tail_chunk(self):
        result = self.call("read_text_chunk", {
            "path": str(self.modified), "offset": -1, "max_bytes": 32
        })
        self.assertIn("new", str(result))

    def test_real_whole_file_digest(self):
        expected = sha256(self.original.read_bytes()).hexdigest()
        result = self.call("sha256_file", {"path": str(self.original)})
        self.assertIn(expected, str(result))

    def test_real_path_metadata(self):
        result = self.call("path_info", {"path": str(self.original)})
        self.assertIn(sha256(self.original.read_bytes()).hexdigest(), str(result))

    def test_real_comparison(self):
        result = self.call("compare_text_files", {
            "before_path": str(self.original),
            "after_path": str(self.modified),
        })
        self.assertIn("-first = 1", str(result))
        self.assertIn("+first = 2", str(result))

    def test_real_folder_pagination(self):
        result = self.call("list_folder_page", {
            "path": str(self.root), "offset": 0, "limit": 1
        })
        self.assertIn("alpha.py", str(result))

    def test_no_roots_denies_every_read(self):
        server = build_server(SimpleNamespace(policy=SimpleNamespace(roots=(), read_roots=())))
        cases = [
            ("read_many_text_files", {"paths": [str(self.original)]}),
            ("find_files", {"path": str(self.root), "name": "alpha"}),
            ("search_code", {"path": str(self.root), "text": "HELLO_SYMBOL"}),
            ("read_text_page", {"path": str(self.original)}),
            ("read_text_lines", {"path": str(self.original)}),
            ("read_text_chunk", {"path": str(self.original)}),
            ("sha256_file", {"path": str(self.original)}),
            ("path_info", {"path": str(self.original)}),
            ("compare_text_files", {
                "before_path": str(self.original), "after_path": str(self.modified)
            }),
            ("list_folder_page", {"path": str(self.root)}),
        ]
        for name, data in cases:
            with self.subTest(name=name), self.assertRaises(ToolError):
                asyncio.run(server.call_tool(name, data))

    def test_outside_root_denies_and_does_not_leak_path(self):
        with inherited_test_temp() as outside:
            foreign = Path(outside) / "secret.py"
            foreign.write_text("SECRET_PRIVATE_MARKER", encoding="utf-8")
            with self.assertRaises(ToolError) as context:
                self.call("read_many_text_files", {
                    "paths": [str(self.original), str(foreign)]
                })
            self.assertNotIn("SECRET_PRIVATE_MARKER", str(context.exception))
            self.assertNotIn(str(foreign), str(context.exception))
            with self.assertRaises(ToolError):
                self.call("sha256_file", {"path": str(foreign)})

    def test_symlink_target_is_rejected_if_supported(self):
        linked = self.root / "linked.py"
        try:
            linked.symlink_to(self.original)
            # The inherited fixture refuses links; remove ours first.
            self.addCleanup(linked.unlink)
        except (OSError, NotImplementedError):
            self.skipTest("No unprivileged symlink permission")
        with self.assertRaises(ToolError):
            self.call("read_many_text_files", {"paths": [str(linked)]})


if __name__ == "__main__":
    unittest.main()
