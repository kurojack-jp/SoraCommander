"""Read-only Owner-scoped paged directory listing through the MCP SDK."""
import asyncio
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
import unittest

from sora_commander_next.file_reads import (
    MAX_PAGED_FOLDER_ITEMS, ReadBoundaryError, list_folder, list_folder_page)
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import _validate_arguments, build_server


class FolderPageTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
        t = self.enterContext(inherited_test_temp())
        self.root = Path(t).resolve()
        self.folder = self.root / "project"
        self.folder.mkdir()

    def test_entire_project_folder_over_200_entries_has_no_gaps(self):
        names = [f"item-{i:04d}.py" for i in range(260)] + ["日本語.txt"]
        for name in names:
            (self.folder / name).write_text("data", encoding="utf-8")
        legacy = list_folder(str(self.folder), (str(self.root),), limit=200)
        self.assertTrue(legacy["truncated"])
        offset, got = 0, []
        for _ in range(5):
            page = list_folder_page(str(self.folder), (str(self.root),),
                                    offset=offset, limit=75)
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["total_entries"], len(names))
            got.extend(item["name"] for item in page["entries"])
            offset = page["next_offset"]
            if not page["truncated"]:
                break
        self.assertEqual(got, sorted(names, key=lambda x: (x.casefold(), x)))
        self.assertEqual(offset, len(names))
        end = list_folder_page(str(self.folder), (str(self.root),),
                               offset=offset, limit=1)
        self.assertEqual(end["entries"], [])
        self.assertFalse(end["truncated"])

    def test_symlink_to_foreign_path_is_filtered_not_counted(self):
        (self.folder / "ordinary.txt").write_text("hi", encoding="utf-8")
        with inherited_test_temp() as outside:
            foreign = Path(outside) / "private.txt"
            foreign.write_text("private", encoding="utf-8")
            link = self.folder / "shortcut.txt"
            try:
                link.symlink_to(foreign)
                # The inherited fixture refuses links; remove ours first.
                self.addCleanup(link.unlink)
            except (NotImplementedError, OSError):
                self.skipTest("symlink creation not available")
            listing = list_folder_page(str(self.folder), (str(self.root),))
            self.assertEqual(listing["entries"], [{"name": "ordinary.txt", "type": "file"}])
            self.assertEqual(listing["total_entries"], 1)

    def test_filtered_links_still_consume_enumeration_budget(self):
        from unittest.mock import patch
        seen = []
        def entries(_path):
            for i in range(20):
                seen.append(i)
                yield self.folder / str(i)
        with patch("sora_commander_next.file_reads.MAX_PAGED_FOLDER_ITEMS", 3), patch.object(Path, "iterdir", entries), patch.object(Path, "is_symlink", return_value=True), patch("sora_commander_next.file_reads._check", return_value=self.folder):
            with self.assertRaises(ReadBoundaryError):
                list_folder_page(str(self.folder), (str(self.root),))
        self.assertEqual(len(seen), 4)

    def test_invalid_page_inputs_are_refused(self):
        for offset in (True, None, 0.5, "2", -1, MAX_PAGED_FOLDER_ITEMS + 1):
            with self.subTest(offset=offset), self.assertRaises(ReadBoundaryError):
                list_folder_page(str(self.folder), (str(self.root),), offset=offset)
        for limit in (True, None, "10", 0, 201):
            with self.subTest(limit=limit), self.assertRaises(ReadBoundaryError):
                list_folder_page(str(self.folder), (str(self.root),), limit=limit)
        with self.assertRaises(ReadBoundaryError):
            list_folder_page(str(self.folder), (str(self.root),), offset=1)

    def test_outside_folder_is_not_exposed(self):
        with inherited_test_temp() as outside:
            with self.assertRaises(ReadBoundaryError):
                list_folder_page(outside, (str(self.root),))

    def test_mcp_tool_returns_pages_and_denies_bad_wire_args(self):
        for i in range(220):
            (self.folder / f"x{i:04}.txt").write_text("ok", encoding="utf-8")
        host = Host(policy=Policy(roots=(str(self.root),)))
        try:
            server = build_server(host)
            out = asyncio.run(server.call_tool(
                "list_folder_page", {"path": str(self.folder), "offset": 200, "limit": 40}))
            self.assertIn("next_offset", str(out))
            self.assertIn("truncated", str(out))
            for bad in ({"offset": True}, {"offset": -1},
                        {"limit": "4"}, {"offset": 10001}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    _validate_arguments("list_folder_page", {"path": str(self.folder), **bad})
        finally:
            host.jobs.close()

    def test_empty_owner_roots_deny_registered_tool(self):
        host = Host()
        try:
            from mcp.server.fastmcp.exceptions import ToolError
            server = build_server(host)
            with self.assertRaises(ToolError):
                asyncio.run(server.call_tool("list_folder_page",
                                             {"path": str(self.folder)}))
        finally:
            host.jobs.close()


if __name__ == "__main__":
    unittest.main()
