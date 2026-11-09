"""Option B: real navigation IO/SDK with abstract, exact path admission.

Production path-policy/link safety is tested separately, without this mock.
"""

import asyncio
from hashlib import sha256
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import mock_read_boundary
import unittest
from unittest.mock import patch

from sora_commander_next.file_reads import (
    ReadBoundaryError, find_files, search_code, path_info,
)
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import _validate_arguments, build_server


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(inherited_test_temp())).resolve()
        self.enterContext(mock_read_boundary(self.root, (
            "src", "src/sub", "src/alpha.py", "src/sub/beta.py",
            "binary.bin", "large.bin", "large.py", "one-giant-line.txt",
        )))
        self.src = self.root / "src"
        self.src.mkdir()
        (self.src / "sub").mkdir()
        self.alpha = self.src / "alpha.py"
        self.alpha.write_text("first line\nneedle for search\nfinal line\n", encoding="utf-8")
        self.beta = self.src / "sub" / "beta.py"
        self.beta.write_text("日本語 needle\nsecond needle\n", encoding="utf-8")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "internal.py").write_text("needle", encoding="utf-8")
        (self.root / ".env").write_text("needle", encoding="utf-8")
        (self.root / "binary.bin").write_bytes(b"\xffneedle")

    def test_casefold_excerpt_keeps_original_match(self):
        self.alpha.write_text("ß" * 400 + "needle" + "x" * 400, encoding="utf-8")
        result = search_code(str(self.src), (str(self.root),), text="NEEDLE")
        hit = next(m for m in result["matches"] if m["path"] == "alpha.py")
        self.assertIn("needle", hit["excerpt"])
        self.alpha.write_text("x" * 400 + "Straße" + "x" * 400, encoding="utf-8")
        hit = search_code(str(self.src), (str(self.root),), text="STRASSE")["matches"][0]
        self.assertIn("Straße", hit["excerpt"])
        self.assertLessEqual(len(hit["excerpt"]), 300)

    def test_hidden_entries_consume_traversal_budget_before_sort(self):
        seen = []
        def entries(_path):
            for i in range(20):
                seen.append(i)
                yield self.root / f".hidden-{i}"
        with patch("sora_commander_next.file_reads._MAX_NAV_VISITS", 3), patch.object(Path, "iterdir", entries):
            with self.assertRaises(ReadBoundaryError):
                find_files(str(self.root), (str(self.root),), name="anything")
        self.assertEqual(len(seen), 4)

    def test_build_directory_skip_is_case_insensitive(self):
        folder = self.root / "NODE_MODULES"
        folder.mkdir()
        (folder / "private.py").write_text("needle", encoding="utf-8")
        result = search_code(str(self.root), (str(self.root),), text="needle")
        self.assertFalse(any("NODE_MODULES" in m["path"] for m in result["matches"]))

    def test_search_uses_opened_size_for_aggregate_budget(self):
        import os
        from types import SimpleNamespace
        real_fstat = os.fstat
        def grown(fd):
            meta = real_fstat(fd)
            return SimpleNamespace(st_mode=meta.st_mode, st_nlink=1, st_size=100)
        # Directory metadata passes the early check; the opened file grew.
        with patch("sora_commander_next.file_reads._MAX_SEARCH_BYTES", 90), patch("os.fstat", grown):
            result = search_code(str(self.src), (str(self.root),), text="needle")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["scanned_files"], 0)
        self.assertEqual(result["matches"], [])

    def test_search_and_metadata_reject_named_file_swap_after_read(self):
        from contextlib import contextmanager
        real_open = Path.open
        for operation in (lambda: search_code(str(self.src), (str(self.root),), text="needle"),
                          lambda: path_info(str(self.alpha), (str(self.root),))):
            backup = self.root / "saved.py"
            @contextmanager
            def swapped(path, *args, **kwargs):
                with real_open(path, *args, **kwargs) as stream:
                    yield stream
                if path == self.alpha and args == ("rb",):
                    path.rename(backup)
                    with real_open(path, "wb") as stream:
                        stream.write(b"replacement")
            with patch.object(Path, "open", swapped), self.assertRaises(ReadBoundaryError):
                operation()
            self.alpha.unlink()
            backup.rename(self.alpha)

    def test_line_output_budget_counts_utf8_bytes(self):
        from sora_commander_next.file_reads import read_text_lines
        self.alpha.write_bytes(("あ" * 3000 + "\n").encode("utf-8") * 2)
        result = read_text_lines(str(self.alpha), (str(self.root),))
        self.assertEqual(len(result["lines"]), 1)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(sum(len(row["text"].encode("utf-8")) for row in result["lines"]), 16384)
        self.alpha.write_bytes(("あ" * 6000).encode("utf-8"))
        with self.assertRaises(ReadBoundaryError):
            read_text_lines(str(self.alpha), (str(self.root),))

    def test_filename_lookup_nested_and_hidden_omitted(self):
        result = find_files(str(self.root), (str(self.root),), name=".py")
        self.assertEqual([x["path"] for x in result["matches"]],
                         ["src/alpha.py", "src/sub/beta.py"])
        self.assertFalse(result["truncated"])
        self.assertEqual(find_files(str(self.root), (str(self.root),),
                                    name="BETA")["matches"][0]["path"],
                         "src/sub/beta.py")

    def test_code_search_exact_lines_relative_paths_and_unicode(self):
        result = search_code(str(self.root), (str(self.root),), text="needle")
        lines = {(m["path"], m["line"]) for m in result["matches"]}
        self.assertEqual(lines, {
            ("src/alpha.py", 2), ("src/sub/beta.py", 1),
            ("src/sub/beta.py", 2),
        })
        self.assertNotIn("internal.py", repr(result))
        self.assertNotIn(".env", repr(result))
        self.assertFalse(result["truncated"])
        self.assertTrue(result["scanned_files"] >= 2)

    def test_no_process_or_shell_called(self):
        with patch("subprocess.run") as run, patch("subprocess.Popen") as launch:
            find_files(str(self.root), (str(self.root),), name=".py")
            search_code(str(self.root), (str(self.root),), text="needle")
            path_info(str(self.alpha), (str(self.root),))
            run.assert_not_called()
            launch.assert_not_called()

    def test_limits_depth_and_truncation(self):
        result = find_files(str(self.root), (str(self.root),),
                            name=".py", max_results=1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["count"], 1)
        result = search_code(str(self.root), (str(self.root),),
                             text="needle", max_results=1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(search_code(str(self.root), (str(self.root),),
                                     text="needle", depth=0)["count"], 0)

    def test_file_info_reports_exact_hash_and_large_file_size(self):
        raw = self.alpha.read_bytes()
        result = path_info(str(self.alpha), (str(self.root),))
        self.assertEqual(result["type"], "file")
        self.assertEqual(result["sha256"], sha256(raw).hexdigest())
        self.assertEqual(result["size_bytes"], len(raw))
        self.assertIs(type(result["modified_ns"]), int)
        self.assertEqual(path_info(str(self.src), (str(self.root),))["type"],
                         "directory")
        big = self.root / "large.bin"
        big.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
        result = path_info(str(big), (str(self.root),))
        self.assertEqual(result["size_bytes"], 4 * 1024 * 1024 + 1)
        self.assertIsNone(result["sha256"])

    def test_wrong_types_and_invalid_limits_denied(self):
        for value in (None, True, 1, "", "\0", "x" * 121):
            with self.subTest(value=value), self.assertRaises(ReadBoundaryError):
                find_files(str(self.root), (str(self.root),), name=value)
        for value in ("x\n", "", "\0", True, None):
            with self.subTest(value=value), self.assertRaises(ReadBoundaryError):
                search_code(str(self.root), (str(self.root),), text=value)
        for kwarg in ({"depth": True}, {"depth": -1}, {"depth": 13},
                      {"max_results": True}, {"max_results": 0},
                      {"max_results": 101}):
            with self.subTest(kwarg=kwarg), self.assertRaises(ReadBoundaryError):
                find_files(str(self.root), (str(self.root),),
                           name=".py", **kwarg)
            with self.subTest(kwarg=kwarg), self.assertRaises(ReadBoundaryError):
                search_code(str(self.root), (str(self.root),),
                            text="needle", **kwarg)

    def test_mock_outside_scope_and_undeclared_alias_denied(self):
        # Abstract rejection, not OS symlink acceptance. No physical link is
        # left in the inherited fixture (whose cleanup rejects links).
        with inherited_test_temp() as outside:
            foreign = Path(outside).resolve()
            (foreign / "private.py").write_text("needle", encoding="utf-8")
            for fn, kwargs in ((find_files, {"name": ".py"}),
                               (search_code, {"text": "needle"})):
                with self.assertRaises(ReadBoundaryError):
                    fn(str(foreign), (str(self.root),), **kwargs)
            with self.assertRaises(ReadBoundaryError):
                path_info(str(foreign / "private.py"), (str(self.root),))
            link = self.root / "alias"
            # Real directory IO, simulated link metadata: scanner must skip it
            # before admission. This does not certify actual OS link handling.
            link.mkdir()
            (link / "private.py").write_text("synthetic needle", encoding="utf-8")
            real_is_symlink = Path.is_symlink
            with patch.object(Path, "is_symlink",
                              lambda p: p == link or real_is_symlink(p)):
                results = find_files(str(self.root), (str(self.root),),
                                     name="private.py")
            self.assertEqual(results["matches"], [])
            with self.assertRaises(ReadBoundaryError):
                search_code(str(link), (str(self.root),), text="needle")

    def test_skip_large_search_file_and_binary(self):
        big = self.root / "large.py"
        big.write_bytes(b"needle" + b"x" * (1024 * 1024))
        out = search_code(str(self.root), (str(self.root),), text="needle")
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["skipped_large"], 1)

    def test_registered_mcp_tools_read_real_tree(self):
        host = Host(policy=Policy(roots=(str(self.root),)))
        try:
            server = build_server(host)
            for name, args in (
                ("find_files", {"path": str(self.root), "name": "alpha.py"}),
                ("search_code", {"path": str(self.root), "text": "needle"}),
                ("path_info", {"path": str(self.alpha)}),
            ):
                with self.subTest(tool=name):
                    out = asyncio.run(server.call_tool(name, args))
                    self.assertTrue(str(out))
                    self.assertFalse(getattr(out, "isError", False))
            for name, arg in (
                ("find_files", {"name": 7}),
                ("search_code", {"text": "bad\nquery"}),
                ("find_files", {"name": "ok", "depth": True}),
                ("search_code", {"text": "ok", "max_results": 101}),
                ("path_info", {"path": 0}),
            ):
                with self.subTest(wire=name, arg=arg), self.assertRaises(ValueError):
                    _validate_arguments(name, {"path": str(self.root), **arg})
        finally:
            host.jobs.close()

    def test_numbered_line_paging_after_code_search(self):
        from sora_commander_next.file_reads import read_text_lines
        match = search_code(str(self.root), (str(self.root),),
                            text="needle")["matches"][0]
        result = read_text_lines(str(self.root / match["path"]),
                                 (str(self.root),), start_line=match["line"],
                                 max_lines=2)
        self.assertEqual([x["line"] for x in result["lines"]], [2, 3])
        self.assertEqual(result["lines"][0]["text"].replace("\r\n", "\n"),
                         "needle for search\n")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["next_line"], 4)
        self.assertEqual(result["sha256"],
                         sha256(self.alpha.read_bytes()).hexdigest())

    def test_line_reader_rejects_oversize_line_outside_and_bad_bound(self):
        from sora_commander_next.file_reads import read_text_lines
        for start in (0, -1, True, "1", 1000001):
            with self.subTest(start=start), self.assertRaises(ReadBoundaryError):
                read_text_lines(str(self.alpha), (str(self.root),),
                                start_line=start)
        for max_lines in (0, True, None, "3", 101):
            with self.subTest(max_lines=max_lines), self.assertRaises(ReadBoundaryError):
                read_text_lines(str(self.alpha), (str(self.root),),
                                max_lines=max_lines)
        with self.assertRaises(ReadBoundaryError):
            read_text_lines(str(self.alpha), (str(self.root),), start_line=99)
        big_line = self.root / "one-giant-line.txt"
        big_line.write_text("x" * 17000, encoding="utf-8")
        with self.assertRaises(ReadBoundaryError):
            read_text_lines(str(big_line), (str(self.root),))
        with inherited_test_temp() as outside:
            file = Path(outside) / "secret.py"
            file.write_text("secret", encoding="utf-8")
            with self.assertRaises(ReadBoundaryError):
                read_text_lines(str(file), (str(self.root),))

    def test_line_reader_registered_via_real_sdk(self):
        host = Host(policy=Policy(roots=(str(self.root),)))
        try:
            server = build_server(host)
            out = asyncio.run(server.call_tool(
                "read_text_lines", {"path": str(self.alpha),
                                    "start_line": 2, "max_lines": 2}))
            self.assertIn("needle for search", str(out))
            self.assertIn("next_line", str(out))
            for bad in ({"start_line": True}, {"max_lines": "4"},
                        {"max_lines": 101}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    _validate_arguments("read_text_lines",
                                        {"path": str(self.alpha), **bad})
        finally:
            host.jobs.close()

    def test_default_host_denies_all_three_tools(self):
        from mcp.server.fastmcp.exceptions import ToolError
        host = Host()
        try:
            server = build_server(host)
            for name, args in (
                ("find_files", {"path": str(self.root), "name": "alpha"}),
                ("search_code", {"path": str(self.root), "text": "needle"}),
                ("path_info", {"path": str(self.alpha)}),
            ):
                with self.subTest(name=name), self.assertRaises(ToolError):
                    asyncio.run(server.call_tool(name, args))
        finally:
            host.jobs.close()


if __name__ == "__main__":
    unittest.main()
