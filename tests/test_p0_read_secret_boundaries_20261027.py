"""Issue #2: secret-bearing paths are not authorized by a generic read root.

Disposable temporary fixtures only. No actual credentials, tokens, network,
Owner authorization, production paths or Windows protection state are used.
"""
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
import unittest

from sora_commander_next import file_reads as reads


class ReadSecretBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
        self.temp = self.enterContext(inherited_test_temp())
        self.root = Path(self.temp).resolve()
        self.roots = (str(self.root),)
        self.safe = self.root / "source.py"
        self.safe.write_text("REVIEW_SAFE_MARKER = 1\n", encoding="utf-8")
        self.normal_json = self.root / "fixture.json"
        self.normal_json.write_text('{"valid":"REVIEW_SAFE_MARKER"}\n', encoding="utf-8")
        self.log = self.root / "trace.log"
        self.log.write_text("REVIEW_SAFE_MARKER\n", encoding="utf-8")
        self.protected = (
            ".env", ".ENV", ".env.local", ".gitignore", ".git",
            "credentials.json", "secret.txt", "token.json", "api_key.toml",
            "owner-output-archive.json", "owner-command-journal.json",
            "Private.PEM", "private.KEY", "publisher.pfx",
            "journal.db", "store.sqlite", "state.wal",
            "backup.zip", "backup.tar.gz", "archive.bak",
        )
        for name in self.protected:
            if name == ".git":
                (self.root / name).mkdir()
                (self.root / name / "config").write_text("REVIEW_SECRET_SENTINEL", encoding="utf-8")
            else:
                (self.root / name).write_text("REVIEW_SECRET_SENTINEL\n", encoding="utf-8")
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "looks_like_code.py").write_text(
            "REVIEW_SECRET_SENTINEL", encoding="utf-8")

    def test_denies_secret_paths_in_every_direct_reader(self):
        readers = (
            lambda p: reads.read_text_file(p, self.roots),
            lambda p: reads.read_text_page(p, self.roots),
            lambda p: reads.read_text_lines(p, self.roots),
            lambda p: reads.read_text_chunk(p, self.roots),
            lambda p: reads.path_info(p, self.roots),
            lambda p: reads.sha256_file(p, self.roots),
            lambda p: reads.read_many_text_files([p], self.roots),
            lambda p: reads.compare_text_files(str(self.safe), p, self.roots),
        )
        for name in self.protected:
            if name == ".git":
                name += "/config"
            location = str(self.root / name)
            for reader in readers:
                with self.subTest(name=name, reader=reader), self.assertRaises(
                        reads.ReadBoundaryError):
                    reader(location)
        with self.assertRaises(reads.ReadBoundaryError):
            reads.read_text_file(str(self.root / "secrets" / "looks_like_code.py"), self.roots)

    def test_batch_and_compare_have_no_partial_success(self):
        with self.assertRaises(reads.ReadBoundaryError):
            reads.read_many_text_files(
                [str(self.safe), str(self.root / "credentials.json")], self.roots)
        with self.assertRaises(reads.ReadBoundaryError):
            reads.compare_text_files(
                str(self.root / "private.KEY"), str(self.safe), self.roots)

    def test_listing_paging_and_discovery_never_reveal_secret_names(self):
        plain = reads.list_folder(str(self.root), self.roots, limit=200)
        paged = reads.list_folder_page(str(self.root), self.roots, limit=200)
        all_names = [v["name"] for v in plain["entries"]]
        self.assertEqual(all_names, [v["name"] for v in paged["entries"]])
        self.assertEqual(paged["total_entries"], len(all_names))
        self.assertIn("source.py", all_names)
        self.assertIn("fixture.json", all_names)
        self.assertIn("trace.log", all_names)
        for name in (*self.protected, "secrets"):
            self.assertNotIn(name, all_names, name)
        self.assertEqual(reads.find_files(
            str(self.root), self.roots, name="REVIEW_SECRET_SENTINEL")["count"], 0)
        self.assertEqual(reads.find_files(
            str(self.root), self.roots, name="credentials")["count"], 0)
        self.assertEqual(reads.find_files(
            str(self.root), self.roots, name="looks_like_code")["count"], 0)
        self.assertEqual(reads.search_code(
            str(self.root), self.roots, text="REVIEW_SECRET_SENTINEL")["count"], 0)
        result = reads.search_code(str(self.root), self.roots, text="REVIEW_SAFE_MARKER")
        self.assertGreaterEqual(result["count"], 1)
        self.assertTrue(all("SECRET_SENTINEL" not in x["excerpt"]
                            for x in result["matches"]))

    def test_pagination_filters_before_offset_and_limit(self):
        first = reads.list_folder_page(str(self.root), self.roots, offset=0, limit=1)
        second = reads.list_folder_page(str(self.root), self.roots, offset=1, limit=1)
        self.assertEqual(first["next_offset"], 1)
        self.assertNotEqual(first["entries"], second["entries"])
        self.assertEqual(first["total_entries"], second["total_entries"])
        self.assertEqual(first["total_entries"], 3)

    def test_normal_source_json_and_logs_remain_readable(self):
        for p in (self.safe, self.normal_json, self.log):
            with self.subTest(path=p.name):
                self.assertIn("REVIEW_SAFE_MARKER", reads.read_text_file(str(p), self.roots)["content"])
                self.assertIsNotNone(reads.sha256_file(str(p), self.roots)["sha256"])
        self.assertEqual(reads.read_many_text_files(
            [str(self.safe), str(self.normal_json)], self.roots)["count"], 2)
        self.assertIsInstance(reads.compare_text_files(
            str(self.safe), str(self.normal_json), self.roots)["diff"], str)

    def test_lexical_parent_traversal_and_ads_rejected(self):
        # Reject before Windows name resolution (even when a stream exists).
        for path in (str(self.root / ".." / self.root.name / "source.py"),
                     str(self.safe) + ":stream",
                     str(self.root / "source.py:stream")):
            with self.subTest(path=path), self.assertRaises(reads.ReadBoundaryError):
                reads.read_text_file(path, self.roots)

    def test_secret_directory_cannot_become_an_authorized_root(self):
        hidden = self.root / "secrets" / "looks_like_code.py"
        for root in (self.root / "secrets", self.root / ".git"):
            with self.subTest(root=root), self.assertRaises(reads.ReadBoundaryError):
                reads.list_folder(str(root), (str(root),))
        with self.assertRaises(reads.ReadBoundaryError):
            reads.read_text_file(str(hidden), (str(self.root / "secrets"),))
        nested = self.root / "secrets" / "nested"
        nested.mkdir()
        source = nested / "source.py"
        source.write_text("REVIEW_SECRET_SENTINEL", encoding="utf-8")
        with self.assertRaises(reads.ReadBoundaryError):
            reads.read_text_file(str(source), (str(nested),))

    def test_link_to_secret_is_rejected_when_os_allows_symlink(self):
        target = self.root / "credentials.json"
        link = self.root / "alias.py"
        try:
            link.symlink_to(target)
            # The inherited fixture refuses links; remove ours first.
            self.addCleanup(link.unlink)
        except (OSError, NotImplementedError):
            self.skipTest("OS symlink creation not permitted")
        with self.assertRaises(reads.ReadBoundaryError):
            reads.read_text_file(str(link), self.roots)
        self.assertNotIn("alias.py", [
            v["name"] for v in reads.list_folder(str(self.root), self.roots)["entries"]])


if __name__ == "__main__":
    unittest.main()
