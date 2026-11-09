"""Isolated synthetic scratch tests; no GUI, audio, credentials or ACL writes."""
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
import stat
import unittest
from unittest.mock import patch

from tests import _inherited_test_temp as fixture


class InheritedTestTempTests(unittest.TestCase):
    def test_positive_nested_files_and_cleanup(self):
        with fixture.inherited_test_temp() as name:
            child = Path(name)
            self.assertEqual(child.parent, fixture._PARENT)
            (child / "nested").mkdir()
            (child / "nested" / "fake.txt").write_text("synthetic", encoding="utf-8")
            self.assertEqual((child / "nested" / "fake.txt").read_text(), "synthetic")
        self.assertFalse(child.exists())

    def test_body_error_cleans_up(self):
        with self.assertRaisesRegex(ValueError, "synthetic"):
            with fixture.inherited_test_temp() as name:
                raise ValueError("synthetic")
        self.assertFalse(Path(name).exists())

    def test_collision_never_removes_existing_child(self):
        with fixture.inherited_test_temp() as name:
            suffix = Path(name).name.removeprefix("scn-synthetic-")
            with patch.object(fixture.secrets, "token_hex", return_value=suffix):
                with self.assertRaises(FileExistsError):
                    with fixture.inherited_test_temp():
                        self.fail("collision accepted")
            self.assertTrue(Path(name).is_dir())

    def test_changed_identity_refuses_cleanup(self):
        with fixture.inherited_test_temp() as name:
            with self.assertRaises(fixture.FixtureHold):
                fixture._verify(Path(name), (-1, -1))
            self.assertTrue(Path(name).exists())

    def test_unverifiable_acl_prevents_creation(self):
        with patch.object(fixture, "_acl", side_effect=fixture.FixtureHold("FAIL/HOLD")), \
                patch.object(Path, "mkdir") as mkdir:
            with self.assertRaises(fixture.FixtureHold):
                with fixture.inherited_test_temp():
                    self.fail("unsafe ACL accepted")
            mkdir.assert_not_called()

    def test_synthetic_denial_never_attempts_cleanup(self):
        # No actual denied OS operation: model the policy without leaving files.
        with patch.object(fixture, "_identity", return_value=(1, 2)), \
                patch.object(fixture, "_verify"), \
                patch.object(fixture, "_pin", side_effect=lambda *args: nullcontext()), \
                patch.object(fixture, "_acl", return_value=b"fake"), \
                patch.object(Path, "mkdir"), \
                patch.object(fixture, "_cleanup") as cleanup:
            with self.assertRaisesRegex(PermissionError, "synthetic"):
                with fixture.inherited_test_temp():
                    raise PermissionError("synthetic")
            cleanup.assert_not_called()

    def test_reparse_symlink_and_hardlink_objects_refused(self):
        for mode, attributes, links in (
                (stat.S_IFDIR, 0x400, 1),
                (stat.S_IFLNK, 0, 1),
                (stat.S_IFREG, 0, 2)):
            with self.subTest(mode=mode, attributes=attributes, links=links):
                info = SimpleNamespace(st_mode=mode, st_file_attributes=attributes,
                                       st_ino=1, st_dev=1, st_nlink=links)
                with patch.object(Path, "lstat", return_value=info):
                    with self.assertRaises(fixture.FixtureHold):
                        fixture._identity(fixture._PARENT,
                                          directory=mode == stat.S_IFDIR)

    def test_canonical_redirection_refused(self):
        with patch.object(Path, "resolve", return_value=Path("unexpected")):
            with self.assertRaises(fixture.FixtureHold):
                fixture._identity(fixture._PARENT, directory=True)

    def test_parent_acl_change_prevents_any_deletion(self):
        with patch.object(fixture, "_verify"), \
                patch.object(fixture, "_acl", return_value=b"changed"), \
                patch.object(Path, "rmdir") as remove:
            with self.assertRaises(fixture.FixtureHold):
                fixture._cleanup(fixture._PARENT / "fake", (1, 2), (1, 3), b"old")
            remove.assert_not_called()

    def test_unknown_tree_object_prevents_any_deletion(self):
        with patch.object(fixture, "_verify"), \
                patch.object(fixture, "_acl", return_value=b"fake"), \
                patch.object(fixture, "_pin", side_effect=lambda *args: nullcontext()), \
                patch.object(Path, "iterdir", return_value=iter([Path("unknown")])), \
                patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG)), \
                patch.object(fixture, "_identity", side_effect=fixture.FixtureHold("unknown")), \
                patch.object(Path, "unlink") as unlink, \
                patch.object(Path, "rmdir") as rmdir:
            with self.assertRaises(fixture.FixtureHold):
                fixture._cleanup(fixture._PARENT / "fake", (1, 2), (1, 3), b"fake")
            unlink.assert_not_called()
            rmdir.assert_not_called()

    def test_cleanup_synthetic_denial_is_not_retried(self):
        with patch.object(fixture, "_verify"), \
                patch.object(fixture, "_acl", return_value=b"fake"), \
                patch.object(fixture, "_pin", side_effect=lambda *args: nullcontext()), \
                patch.object(Path, "iterdir", return_value=iter([])), \
                patch.object(Path, "rmdir", side_effect=PermissionError("synthetic")) as rmdir:
            with self.assertRaisesRegex(PermissionError, "synthetic"):
                fixture._cleanup(fixture._PARENT / "fake", (1, 2), (1, 3), b"fake")
            rmdir.assert_called_once()

    def test_cleanup_outside_fixed_parent_is_refused(self):
        with patch.object(Path, "rmdir") as rmdir:
            with self.assertRaises(fixture.FixtureHold):
                fixture._cleanup(fixture._PARENT.parent / "unknown", (1, 2), (1, 3), b"fake")
            rmdir.assert_not_called()
