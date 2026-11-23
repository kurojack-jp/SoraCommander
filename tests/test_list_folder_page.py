"""Read-only Owner-scoped paged directory listing through the MCP SDK."""
import asyncio
from contextlib import nullcontext
from pathlib import Path
import os
import stat
from types import SimpleNamespace
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
import unittest
from unittest.mock import Mock, patch

from sora_commander_next import file_reads
from sora_commander_next.file_reads import (
    MAX_PAGED_FOLDER_ITEMS, ReadBoundaryError, list_folder, list_folder_page)
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import _validate_arguments, build_server


class PlaceholderListingTests(unittest.TestCase):
    def test_list_page_and_find_data_only_entries(self):
        root = Path.cwd() / "virtual-project"
        reparse = stat.FILE_ATTRIBUTE_REPARSE_POINT
        descriptions = {
            "a-placeholder.py": (reparse, 0x9000001A, 1),
            "b-placeholder.py": (reparse, 0x80000013, 1),
            "junction.py": (reparse, stat.IO_REPARSE_TAG_MOUNT_POINT, 1),
            "symlink.py": (reparse, stat.IO_REPARSE_TAG_SYMLINK, 1),
            "name-surrogate.py": (reparse, 0xA000001F, 1),
            "hidden.py": (stat.FILE_ATTRIBUTE_HIDDEN, 0, 1),
            "system.py": (stat.FILE_ATTRIBUTE_SYSTEM, 0, 1),
            "hardlink.py": (0, 0, 2),
            ".denied.py": (0, 0, 1),
        }
        entries = [root / name for name in reversed(descriptions)]
        children = [SimpleNamespace(name=entry.name, path=str(entry)) for entry in entries]

        def metadata(entry):
            attrs, tag, links = descriptions[entry.name]
            return SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=links,
                                   st_file_attributes=attrs, st_reparse_tag=tag)

        with patch.object(file_reads, "_check", side_effect=lambda path, _: Path(path)), \
                patch.object(Path, "iterdir", return_value=iter(entries)), \
                patch.object(file_reads.os, "scandir", side_effect=lambda _: nullcontext(children)), \
                patch.object(Path, "is_dir", return_value=True), \
                patch.object(Path, "is_symlink", return_value=False), \
                patch.object(Path, "is_junction", return_value=False), \
                patch.object(Path, "lstat", metadata):
            scope = (str(root),)
            listing = list_folder(str(root), scope)
            self.assertEqual([item["name"] for item in listing["entries"]],
                             ["a-placeholder.py", "b-placeholder.py"])
            # Each listing gets a fresh directory iterator.
            with patch.object(Path, "iterdir", return_value=iter(entries)):
                page = list_folder_page(str(root), scope, offset=1, limit=1)
            self.assertEqual(page["total_entries"], 2)
            self.assertEqual(page["entries"], [{"name": "b-placeholder.py", "type": "file"}])
            self.assertEqual(page["next_offset"], 2)
            self.assertFalse(page["truncated"])
            found = file_reads.find_files(str(root), scope, name="placeholder")
            self.assertEqual(found["matches"], [
                {"path": "a-placeholder.py", "type": "file"},
                {"path": "b-placeholder.py", "type": "file"},
            ])

    @unittest.skipUnless(os.name == "nt", "Windows directory enumeration attributes")
    def test_search_checks_enumeration_and_lstat_before_open(self):
        root = Path.cwd() / "virtual-project"
        attrs = {
            "data.py": file_reads.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
            "offline.py": stat.FILE_ATTRIBUTE_OFFLINE,
        }
        children = [SimpleNamespace(name=name, path=str(root / name),
                    stat=Mock(return_value=SimpleNamespace(st_file_attributes=0))) for name in attrs]
        recall_on_open = SimpleNamespace(
            name="open.py", path=str(root / "open.py"),
            stat=Mock(return_value=SimpleNamespace(
                st_file_attributes=file_reads.FILE_ATTRIBUTE_RECALL_ON_OPEN)))
        children.append(recall_on_open)

        def metadata(entry):
            return SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=1,
                                   st_file_attributes=attrs[entry.name])

        with patch.object(file_reads, "_check", side_effect=lambda path, _: Path(path)), \
                patch.object(file_reads.os, "scandir", return_value=nullcontext(children)), \
                patch.object(Path, "is_dir", return_value=True), \
                patch.object(Path, "is_symlink", return_value=False), \
                patch.object(Path, "is_junction", return_value=False), \
                patch.object(Path, "lstat", metadata), patch.object(Path, "open") as opened:
            result = file_reads.search_code(str(root), (str(root),), text="hello")
        recall_on_open.stat.assert_called_once_with(follow_symlinks=False)
        self.assertEqual(result["scanned_files"], 0)
        self.assertEqual(result["matches"], [])
        opened.assert_not_called()


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
        self.assertEqual(legacy["omitted_entries"], 0)
        offset, got = 0, []
        for _ in range(5):
            page = list_folder_page(str(self.folder), (str(self.root),),
                                    offset=offset, limit=75)
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["total_entries"], len(names))
            self.assertEqual(page["omitted_entries"], 0)
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

    def test_listings_count_excluded_entries_before_sorting_or_paging(self):
        protected = self.folder / "a-secret.env"
        excluded = (protected.name, "b-hidden.txt", "c-system.txt", "d-link.txt",
                    "e-reparse.txt", "f-hardlink.txt")
        for name in (*excluded, "Z-last.txt", "g-first.txt"):
            (self.folder / name).touch()
        nested = self.folder / "middle"
        nested.mkdir()
        (nested / ".nested-secret").touch()
        deeper = nested / "deeper"
        deeper.mkdir()
        (deeper / ".unvisited").touch()
        flags = {"b-hidden.txt": stat.FILE_ATTRIBUTE_HIDDEN,
                 "c-system.txt": stat.FILE_ATTRIBUTE_SYSTEM,
                 "e-reparse.txt": stat.FILE_ATTRIBUTE_REPARSE_POINT}
        real_lstat = Path.lstat
        real_is_symlink = Path.is_symlink

        def lstat(path):
            self.assertNotEqual(path, protected)
            if path.parent != self.folder or path.name not in excluded:
                return real_lstat(path)
            return SimpleNamespace(st_mode=stat.S_IFREG,
                                   st_nlink=2 if path.name == "f-hardlink.txt" else 1,
                                   st_file_attributes=flags.get(path.name, 0),
                                   st_reparse_tag=0x20000001 if path.name == "e-reparse.txt" else 0)

        def is_symlink(path):
            self.assertNotEqual(path, protected)
            return path == self.folder / "d-link.txt" or real_is_symlink(path)

        path, roots = str(self.folder), (str(self.root),)
        visible = [{"name": "g-first.txt", "type": "file"},
                   {"name": "middle", "type": "directory"},
                   {"name": "Z-last.txt", "type": "file"}]
        with patch.object(Path, "lstat", lstat), patch.object(Path, "is_symlink", is_symlink):
            self.assertEqual(list_folder(path, roots, limit=1), {
                "entries": visible[:1], "truncated": True, "omitted_entries": 6,
            })
            for offset in range(4):
                with self.subTest(offset=offset):
                    next_offset = min(offset + 1, 3)
                    self.assertEqual(list_folder_page(path, roots, offset=offset, limit=1), {
                        "entries": visible[offset:next_offset], "offset": offset,
                        "next_offset": next_offset, "total_entries": 3,
                        "truncated": next_offset < 3, "omitted_entries": 6,
                    })
            self.assertEqual(list_folder(path, roots, limit=1, depth=1), {
                "entries": [{**visible[0], "path": "g-first.txt"}],
                "truncated": True, "omitted_entries": 7,
            })

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
        (self.folder / "a.env").touch()
        host = Host(policy=Policy(roots=(str(self.root),)))
        try:
            server = build_server(host)
            _, out = asyncio.run(server.call_tool(
                "list_folder_page", {"path": str(self.folder), "offset": 200, "limit": 40}))
            self.assertEqual(out, {
                "entries": [{"name": f"x{i:04}.txt", "type": "file"}
                            for i in range(200, 220)],
                "offset": 200, "next_offset": 220, "total_entries": 220,
                "truncated": False, "omitted_entries": 1,
            })
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
