"""Windows disposable fixtures for single-file and folder read boundaries."""
from hashlib import sha256
import os
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
import unittest
from unittest.mock import patch

from sora_commander_next import file_reads as reads


class SingleReadBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
        self.fixture = self.enterContext(inherited_test_temp())
        self.root = Path(self.fixture).resolve()
        self.roots = (str(self.root),)

    def test_normal_single_file_preserves_content_and_sha(self):
        path = self.root / "normal.txt"
        data = "normal あ\r\n".encode("utf-8")
        path.write_bytes(data)
        result = reads.read_text_file(str(path), self.roots)
        self.assertEqual(result, {"content": data.decode("utf-8"),
                                  "bytes": len(data), "sha256": sha256(data).hexdigest()})

    def test_hardlink_is_rejected_on_any_platform(self):
        # Same rejection as the Windows acceptance test, without the
        # platform gate, so the fix is verified on Linux too.
        original = self.root / "fixture.txt"
        alias = self.root / "alias.txt"
        original.write_text("synthetic fixture", encoding="utf-8")
        try:
            os.link(original, alias)
            # The inherited fixture refuses links; remove ours first.
            self.addCleanup(alias.unlink)
        except (NotImplementedError, OSError):
            self.skipTest("hardlink creation not available")
        self.assertEqual(alias.stat().st_nlink, 2)
        for path in (original, alias):
            with self.subTest(path=path.name), self.assertRaises(reads.ReadBoundaryError):
                reads.read_text_file(str(path), self.roots)

    def test_single_file_size_boundary_is_preserved(self):
        path = self.root / "sized.txt"
        path.write_bytes(b"x" * 65536)
        result = reads.read_text_file(str(path), self.roots)
        self.assertEqual(result["bytes"], 65536)
        path.write_bytes(b"x" * 65537)
        with self.assertRaises(reads.ReadBoundaryError):
            reads.read_text_file(str(path), self.roots)

    @unittest.skipUnless(os.name == "nt", "Windows hardlink acceptance")
    def test_real_windows_hardlink_is_rejected(self):
        original = self.root / "fixture.txt"
        alias = self.root / "alias.txt"
        original.write_text("synthetic fixture", encoding="utf-8")
        os.link(original, alias)
        # The inherited fixture refuses links; remove ours first.
        self.addCleanup(alias.unlink)
        self.assertEqual(alias.stat().st_nlink, 2)
        for path in (original, alias):
            with self.subTest(path=path.name), self.assertRaises(reads.ReadBoundaryError):
                reads.read_text_file(str(path), self.roots)

    def test_list_rejects_visited_limit_including_hidden_entries(self):
        for name in ("normal.txt", ".hidden", "other.txt"):
            (self.root / name).write_text("x", encoding="utf-8")
        with patch.object(reads, "MAX_PAGED_FOLDER_ITEMS", 2):
            for listing in (reads.list_folder, reads.list_folder_page):
                with self.subTest(listing=listing.__name__), self.assertRaises(reads.ReadBoundaryError):
                    listing(str(self.root), self.roots, limit=1)

    def test_list_sort_filter_and_truncation_are_preserved(self):
        for name in ("b.txt", "A.txt", "credentials.json"):
            (self.root / name).write_text("x", encoding="utf-8")
        result = reads.list_folder(str(self.root), self.roots, limit=1)
        self.assertEqual(result, {"entries": [{"name": "A.txt", "type": "file"}],
                                  "truncated": True})


if __name__ == "__main__":
    unittest.main()
