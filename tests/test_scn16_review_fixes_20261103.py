"""Codex independent review 2026-11-03 fixes. NON-SECRET synthetic data only.

P1: a sensitive-looking directory configured as the read root (or its ancestor)
    must not expose its children: credentials.*, secret.*, *.pem, ...
P2: a literal patch anchor must be unique even when matches overlap; str.count()
    counts "aa" in "aaa" once although it occurs at two positions.
"""
from hashlib import sha256
from pathlib import Path
import unittest
from unittest.mock import patch

from sora_commander_next import file_reads as reads
from sora_commander_next.file_edits import preview_source_patch
from sora_commander_next.file_reads import ReadBoundaryError
from sora_commander_next.source_review import (
    SourceConflict, SourcePolicy, SourceReviewWorkflow,
)
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import mock_read_boundary
import test_sol_broker_vertical as fixtures

SENSITIVE_DIRECTORIES = ("credentials.config", "secret.pem", "token.store",
                         "private.key", "vault.kdbx", "signing.pfx")
# Data/archive suffixes are file formats; as a root or ancestor directory they
# are ordinary workspaces (review follow-up 2026-11-03). Below a root the full
# file policy still applies.
ORDINARY_DATA_SUFFIX_DIRECTORIES = ("project.db", "snapshot.bak", "dump.sqlite",
                                    "release.zip", "old.backup")


class SensitiveReadRootTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(inherited_test_temp())).resolve()
        # Exempt only the canonical temp/ fixture ancestor and call the REAL
        # _check directly; canonical_fixture_admission() is stricter than
        # production below the fixture and would hide a production gap.
        self.enterContext(patch.object(
            reads, "_READ_DENY_COMPONENTS", reads._READ_DENY_COMPONENTS - {"temp"}))

    def make(self, *parts):
        folder = self.base.joinpath(*parts)
        folder.mkdir(parents=True)
        (folder / "inner.txt").write_text("synthetic only", encoding="utf-8")
        return folder

    def test_ordinary_root_is_readable_positive_control(self):
        folder = self.make("ordinary")
        self.assertEqual(reads.read_text_file(
            str(folder / "inner.txt"), (str(folder),))["content"], "synthetic only")

    def test_sensitive_directory_as_root_is_denied(self):
        for name in SENSITIVE_DIRECTORIES:
            folder = self.make(name)
            for spelled in (name, name.upper()):
                root = str(self.base / spelled)
                with self.subTest(root=spelled):
                    with self.assertRaises(ReadBoundaryError):
                        reads.read_text_file(str(Path(root) / "inner.txt"), (root,))
                    with self.assertRaises(ReadBoundaryError):
                        reads.list_folder(root, (root,))
            self.assertTrue((folder / "inner.txt").is_file())

    def test_sensitive_ancestor_of_root_is_denied(self):
        for name in SENSITIVE_DIRECTORIES:
            folder = self.make(name, "nested")
            with self.subTest(ancestor=name), self.assertRaises(ReadBoundaryError):
                reads.read_text_file(str(folder / "inner.txt"), (str(folder),))

    def test_data_suffix_directory_as_root_or_ancestor_is_readable(self):
        for name in ORDINARY_DATA_SUFFIX_DIRECTORIES:
            for parts in ((name,), (name, "normal-workspace")):
                folder = self.make(*parts)
                with self.subTest(root=parts):
                    self.assertEqual(reads.read_text_file(
                        str(folder / "inner.txt"), (str(folder),))["content"],
                        "synthetic only")
                    self.assertEqual(reads.list_folder(str(folder), (str(folder),)),
                                     {"entries": [{"name": "inner.txt", "type": "file"}],
                                      "truncated": False})

    def test_data_suffix_below_root_is_still_denied(self):
        root = self.make("ordinary-root")
        for name in ORDINARY_DATA_SUFFIX_DIRECTORIES:
            folder = self.make("ordinary-root", name)
            with self.subTest(child=name), self.assertRaises(ReadBoundaryError):
                reads.read_text_file(str(folder / "inner.txt"), (str(root),))
        for name in ("cache.db", "notes.bak"):
            (root / name).write_text("synthetic only", encoding="utf-8")
            with self.subTest(file=name), self.assertRaises(ReadBoundaryError):
                reads.read_text_file(str(root / name), (str(root),))
        listed = {row["name"] for row in reads.list_folder(str(root), (str(root),))["entries"]}
        self.assertTrue(listed.isdisjoint({*ORDINARY_DATA_SUFFIX_DIRECTORIES,
                                           "cache.db", "notes.bak"}))

    def test_data_suffix_file_as_root_is_still_denied(self):
        target = self.base / "cache.db"
        target.write_text("synthetic only", encoding="utf-8")
        with self.assertRaises(ReadBoundaryError):
            reads.read_text_file(str(target), (str(target),))

    def test_sensitive_names_are_denied_before_filesystem_resolution(self):
        root = str(self.base)
        for name in SENSITIVE_DIRECTORIES:
            target = str(self.base / name / "inner.txt")
            with patch.object(Path, "resolve", side_effect=AssertionError(
                    "sensitive name reached filesystem resolution")):
                with self.subTest(name=name), self.assertRaises(ReadBoundaryError):
                    reads.read_text_file(target, (str(self.base / name),))
                with self.subTest(name=name, root="parent"), \
                        self.assertRaises(ReadBoundaryError):
                    reads.read_text_file(target, (root,))


class OverlappingAnchorTests(unittest.TestCase):
    ORIGINAL = 'value = "aaa"\n'

    def setUp(self):
        self.root = Path(self.enterContext(inherited_test_temp())).resolve()
        self.enterContext(mock_read_boundary(self.root, ("feature.py",), preview=True))
        self.target = self.root / "feature.py"
        self.target.write_bytes(self.ORIGINAL.encode("utf-8"))
        self.digest = sha256(self.target.read_bytes()).hexdigest()

    def test_overlapping_anchor_preview_is_rejected_without_write(self):
        self.assertEqual(self.ORIGINAL.count("aa"), 1)  # The str.count() gap.
        with self.assertRaisesRegex(ReadBoundaryError, "exactly once"):
            preview_source_patch(str(self.target), (str(self.root),),
                                 "aa", "b", self.digest)
        self.assertEqual(self.target.read_bytes(), self.ORIGINAL.encode("utf-8"))

    def test_unique_and_absent_anchor_behaviour_is_unchanged(self):
        row = preview_source_patch(str(self.target), (str(self.root),),
                                   '"aaa"', '"b"', self.digest)
        self.assertEqual(row["content"], 'value = "b"\n')
        with self.assertRaisesRegex(ReadBoundaryError, "exactly once"):
            preview_source_patch(str(self.target), (str(self.root),),
                                 "zzz", "b", self.digest)

    def test_source_review_rejects_overlapping_anchor_before_owner_review(self):
        store = fixtures.AtomicStore()
        original = self.ORIGINAL.encode("utf-8")
        store.seed(fixtures.TARGET, original)
        owner = fixtures.Owner()
        flow = SourceReviewWorkflow(policy=SourcePolicy((fixtures.ALLOW,)),
                                    store=store, owner=owner, clock=lambda: 0.1)
        proposed = self.ORIGINAL.replace("aa", "b", 1).encode("utf-8")
        args = {"path": fixtures.TARGET, "old_text": "aa", "new_text": "b",
                "expected_sha256": sha256(original).hexdigest(),
                "expected_new_sha256": sha256(proposed).hexdigest(),
                "request_id": "e" * 32}
        with self.assertRaises(SourceConflict):
            flow.submit("request_source_patch", args)
        self.assertEqual(store.writes, 0)
        self.assertEqual(owner.reviews, [])
        self.assertEqual(flow._records, {})


if __name__ == "__main__":
    unittest.main()
