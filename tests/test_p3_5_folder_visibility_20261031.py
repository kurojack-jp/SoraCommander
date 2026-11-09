"""P3-5: Win32 hidden/system/hardlink metadata is filtered consistently.

Only synthetic fixtures in repo-local temp/; no Owner GUI, Host restart,
project source files or external directories are modified.
"""
import ctypes
import os
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
import unittest

from sora_commander_next import file_reads as read

ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT / "temp"


@unittest.skipUnless(os.name == "nt", "Win32 file attribute acceptance")
class FolderVisibilityWindowsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
        self.assertTrue(TEMP.is_dir() and not TEMP.is_symlink())
        holder = self.enterContext(inherited_test_temp())
        self.root = Path(holder).resolve()
        self.roots = (str(self.root),)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.get_attrs = self.kernel.GetFileAttributesW
        self.get_attrs.argtypes = (ctypes.c_wchar_p,)
        self.get_attrs.restype = ctypes.c_uint32
        self.set_attrs = self.kernel.SetFileAttributesW
        self.set_attrs.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
        self.set_attrs.restype = ctypes.c_int
        self.changed = []
        self.addCleanup(self.restore_attributes)

    def create(self, name, text="P3_5_SENSITIVE_MARKER"):
        p = self.root / name
        p.write_text(text, encoding="utf-8")
        return p

    def mark(self, path, flag):
        original = self.get_attrs(str(path))
        self.assertNotEqual(original, 0xFFFFFFFF, str(path))
        self.assertTrue(self.set_attrs(str(path), original | flag), str(path))
        self.changed.append((path, original))
        self.assertTrue(path.lstat().st_file_attributes & flag)

    def restore_attributes(self):
        for path, original in reversed(self.changed):
            if path.exists():
                self.assertTrue(self.set_attrs(str(path), original), str(path))

    def test_plain_paged_recursive_and_search_obey_same_win32_exclusions(self):
        self.create("safe-one.py", "P3_5_VISIBLE_MARKER")
        self.create("safe-two.txt", "P3_5_VISIBLE_MARKER")
        hidden = self.create("quiet.py")
        system = self.create("ordinary.py")
        self.mark(hidden, 0x2)  # FILE_ATTRIBUTE_HIDDEN
        self.mark(system, 0x4)  # FILE_ATTRIBUTE_SYSTEM
        hard = self.create("original.py")
        os.link(hard, self.root / "alias.py")
        # The inherited fixture refuses links; remove ours first.
        self.addCleanup((self.root / "alias.py").unlink)
        secret_dir = self.root / "ordinaryfolder"
        secret_dir.mkdir()
        (secret_dir / "nested.py").write_text("P3_5_SENSITIVE_MARKER", encoding="utf-8")
        self.mark(secret_dir, 0x2)
        expected = ["safe-one.py", "safe-two.txt"]
        plain = read.list_folder(str(self.root), self.roots)
        paged = read.list_folder_page(str(self.root), self.roots)
        names_plain = [x["name"] for x in plain["entries"]]
        names_paged = [x["name"] for x in paged["entries"]]
        self.assertEqual(names_plain, expected)
        self.assertEqual(names_paged, expected)
        self.assertEqual(paged["total_entries"], 2)
        self.assertEqual(paged["next_offset"], 2)
        self.assertFalse(paged["truncated"])
        second = read.list_folder_page(str(self.root), self.roots, offset=1, limit=1)
        self.assertEqual([x["name"] for x in second["entries"]], ["safe-two.txt"])
        self.assertEqual(second["next_offset"], 2)
        recursive = read.list_folder(str(self.root), self.roots, depth=1)
        self.assertEqual([x["path"] for x in recursive["entries"]], expected)
        filename_hits = read.find_files(str(self.root), self.roots, name="py")
        self.assertEqual([x["path"] for x in filename_hits["matches"]], ["safe-one.py"])
        source_hits = read.search_code(
            str(self.root), self.roots, text="P3_5_SENSITIVE_MARKER"
        )
        self.assertEqual(source_hits["count"], 0, source_hits)
        visible_hits = read.search_code(
            str(self.root), self.roots, text="P3_5_VISIBLE_MARKER"
        )
        self.assertEqual(visible_hits["count"], 2, visible_hits)

    def test_page_does_not_reveal_hardlinked_filenames(self):
        self.create("safe.txt", "ok")
        target = self.create("original.txt")
        os.link(target, self.root / "alias.txt")
        # The inherited fixture refuses links; remove ours first.
        self.addCleanup((self.root / "alias.txt").unlink)
        result = read.list_folder_page(str(self.root), self.roots)
        self.assertEqual(result["entries"], [{"name": "safe.txt", "type": "file"}])
        self.assertEqual(result["total_entries"], 1)


if __name__ == "__main__":
    unittest.main()
