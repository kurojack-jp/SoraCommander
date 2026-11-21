"""Owner output archive path gate using disposable paths and MOCKED ACL.

No real Windows protected directory, OS security descriptor inspection,
production storage, Owner GUI, Broker, or command launch is performed.
"""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tests._inherited_test_temp import inherited_test_temp
from sora_commander_next.stdio_main import owner_journal_path


class OwnerArchivePathPreflightTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(inherited_test_temp()))
        self.check = self.enterContext(patch(
            "sora_commander_next.stdio_main._verify_owner_journal_acls"))

    def resolve(self):
        try:
            return owner_journal_path(storage_override=self.base)
        finally:
            self.check.assert_called_once_with(self.base)

    def test_existing_regular_archive_and_journal_are_accepted_under_mock_acl(self):
        journal = self.base / "owner-session-journal.json"
        archive = self.base / "owner-output-archive.json"
        journal.write_text('{"version":1,"sessions":[]}', encoding="utf-8")
        archive.write_text('{"version":1,"results":[]}', encoding="utf-8")
        self.assertEqual(self.resolve(), journal)

    def test_preexisting_archive_directory_is_refused(self):
        (self.base / "owner-output-archive.json").mkdir()
        with self.assertRaisesRegex(RuntimeError, "^Owner output archive target invalid$"):
            self.resolve()

    def test_preexisting_hardlinked_archive_is_refused(self):
        target = self.base / "owner-output-archive.json"
        normal = self.base / "safe-fixture.txt"
        normal.write_text("synthetic only", encoding="utf-8")
        os.link(normal, target)
        with self.assertRaisesRegex(RuntimeError, "^Owner output archive target invalid$"):
            self.resolve()
        self.assertTrue(target.samefile(normal))
        target.unlink()

    def test_preexisting_hardlinked_lock_is_refused(self):
        lock = self.base / "owner-session-journal.json.lock"
        source = self.base / "unrelated-fixture.txt"
        source.write_text("synthetic only", encoding="utf-8")
        os.link(source, lock)
        with self.assertRaisesRegex(RuntimeError, "^Owner journal lock target invalid$"):
            self.resolve()
        self.assertTrue(lock.samefile(source))
        lock.unlink()


if __name__ == "__main__":
    unittest.main()
