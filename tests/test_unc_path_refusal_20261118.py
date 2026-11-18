"""Offline path boundary regression tests."""
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sora_commander_next import file_reads
from sora_commander_next.commands import Command, absolute
from sora_commander_next.file_reads import ReadBoundaryError, list_folder, path_info, read_text_file
from sora_commander_next.git_readonly import _file


ROOT = Path(__file__).resolve().parents[1]
REFUSED = (r"\\host\share\f.txt", "//host/share/f.txt", r"\\?\C:\x", r"\\.\pipe\x")


class UncPathRefusalTests(unittest.TestCase):
    def test_reads_refuse_before_filesystem_access(self):
        for path in REFUSED:
            for read in (read_text_file, path_info, list_folder):
                with self.subTest(path=path, read=read.__name__):
                    with patch.object(Path, "resolve", side_effect=AssertionError("resolve called")), \
                            patch.object(os, "stat", side_effect=AssertionError("stat called")), \
                            patch.object(Path, "open", side_effect=AssertionError("open called")):
                        with self.assertRaisesRegex(ReadBoundaryError, "^UNC and device paths are unsupported$"):
                            read(path, (str(ROOT),))

    def test_command_paths_refuse_before_filesystem_access(self):
        exe = str(ROOT / "local.exe")
        for path in REFUSED:
            for call, args in ((absolute, (path,)), (Command, (path, (), str(ROOT))),
                               (Command, (exe, (), path))):
                with self.subTest(path=path, call=call.__name__, args=args):
                    with patch.object(Path, "resolve", side_effect=AssertionError("resolve called")), \
                            patch.object(os, "stat", side_effect=AssertionError("stat called")):
                        with self.assertRaisesRegex(ValueError, "^UNC and device paths are unsupported$"):
                            call(*args)

    def test_git_refuses_link_components_before_resolving(self):
        link = ROOT / "linked"
        folder = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0)
        for info in (SimpleNamespace(st_mode=stat.S_IFLNK),
                     SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                                     st_reparse_tag=stat.IO_REPARSE_TAG_MOUNT_POINT)):
            with self.subTest(info=info):
                with patch.object(Path, "lstat", autospec=True, side_effect=[folder, info]) as lstat, \
                        patch.object(Path, "resolve", side_effect=AssertionError("resolve called")):
                    with self.assertRaisesRegex(ReadBoundaryError, "^FILE_OUTSIDE_REPOSITORY$"):
                        _file(ROOT, "linked/file.txt", (str(ROOT),))
                self.assertEqual([call.args[0] for call in lstat.call_args_list], [ROOT, link])

    def test_local_paths_and_historical_git_names_still_work(self):
        # pyproject.toml exists in both the development checkout and the built public tree.
        path = ROOT / "pyproject.toml"
        roots = (str(path.parent),)
        self.assertEqual(absolute(str(path)), str(path))
        self.assertIn("[project]", read_text_file(str(path), roots)["content"])
        self.assertEqual(path_info(str(path), roots)["type"], "file")
        self.assertIn(path.name, {item["name"] for item in list_folder(str(path.parent), roots)["entries"]})
        missing = "historical-file-unc-regression/deleted.txt"
        self.assertEqual(_file(path.parent, missing, roots), missing)


class NetworkLinkRefusalTests(unittest.TestCase):
    def setUp(self):
        self.link = ROOT / "linked"
        self.path = self.link / "file.txt"
        self.folder = SimpleNamespace(st_mode=stat.S_IFDIR)
        self.symlink = SimpleNamespace(st_mode=stat.S_IFLNK)
        self.junction = SimpleNamespace(
            st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            st_reparse_tag=stat.IO_REPARSE_TAG_MOUNT_POINT)
        self.entries = {self.link: self.symlink}
        self.lstat = self.enterContext(patch.object(
            Path, "lstat", autospec=True, side_effect=lambda path: self.entries.get(path, self.folder)))
        self.readlink = self.enterContext(patch.object(os, "readlink"))
        self.drive_type = self.enterContext(patch.object(file_reads, "_drive_type", return_value=3))
        self.resolve = self.enterContext(patch.object(
            Path, "resolve", autospec=True, side_effect=AssertionError("resolve called")))
        self.enterContext(patch.object(os, "stat", side_effect=AssertionError("stat called")))

    def assert_refused(self, message):
        with self.assertRaisesRegex(ReadBoundaryError, message):
            path_info(str(self.path), (str(ROOT),))
        with self.assertRaisesRegex(ValueError, message):
            absolute(str(self.path))
        self.resolve.assert_not_called()

    def test_network_spelling_is_refused_by_both_callers(self):
        for target in (r"\\server\share", r"\\?\UNC\server\share", "//server/share", r"\\.\pipe\name"):
            with self.subTest(target=target):
                self.readlink.return_value = target
                self.assert_refused("^link leads to a network location$")
        self.drive_type.assert_not_called()
        self.assertNotIn(self.path, [args.args[0] for args in self.lstat.call_args_list])

    @unittest.skipUnless(os.name == "nt", "Windows drive letters required")
    def test_mapped_drive_target_is_refused_by_both_callers(self):
        for target in (r"Z:\folder", r"\\?\Z:\folder"):
            with self.subTest(target=target):
                self.readlink.return_value = target
                self.drive_type.return_value = 4
                self.assert_refused("^link leads to a network location$")
                self.drive_type.assert_called_with("Z:\\")
        self.assertFalse(any(args.args[0].drive.endswith("Z:") for args in self.lstat.call_args_list))

    @unittest.skipUnless(os.name == "nt", "Windows junction target required")
    def test_extended_local_junction_preserves_command_resolution(self):
        self.entries[self.link] = self.junction
        self.readlink.return_value = r"\\?\C:\real\dir"
        expected = Path(r"C:\real\dir\file.txt")
        self.resolve.side_effect = None
        self.resolve.return_value = expected
        self.assertEqual(absolute(str(self.path)), str(expected))
        self.drive_type.assert_called_once_with("C:\\")
        self.assertIn(Path(r"\\?\C:\real\dir\file.txt"),
                      [args.args[0] for args in self.lstat.call_args_list])
        self.resolve.assert_called_once_with(self.path)

    def test_relative_link_chain_is_followed_before_resolution(self):
        second = ROOT / "second"
        self.entries[second] = self.junction
        self.readlink.side_effect = {self.link: "second", second: r"\\server\share"}.__getitem__
        self.assert_refused("^link leads to a network location$")
        self.assertEqual([args.args[0] for args in self.readlink.call_args_list],
                         [self.link, second, self.link, second])

    def test_more_than_40_hops_are_refused_by_both_callers(self):
        self.readlink.return_value = self.link.name
        self.assert_refused("^too many path link hops$")
        self.assertEqual(self.readlink.call_count, 80)

    def test_unreadable_reparse_target_is_refused_without_details(self):
        self.entries[self.link] = self.junction
        self.readlink.side_effect = OSError("synthetic target detail")
        self.assert_refused("^link target could not be inspected$")

    def test_read_refuses_network_link_in_configured_root_before_resolving_it(self):
        path = ROOT / "pyproject.toml"
        self.readlink.return_value = r"\\server\share"
        self.resolve.side_effect = None
        self.resolve.return_value = path
        with self.assertRaisesRegex(ReadBoundaryError, "^link leads to a network location$"):
            path_info(str(path), (str(self.link),))
        self.resolve.assert_called_once_with(path, strict=True)

    @unittest.skipUnless(os.name == "nt", "Windows drive letters required")
    def test_directly_supplied_mapped_drive_is_still_resolved(self):
        path = Path(r"Z:\folder\file.txt")
        self.drive_type.return_value = 4
        self.resolve.side_effect = None
        self.resolve.return_value = path
        self.assertEqual(absolute(str(path)), str(path))
        self.drive_type.assert_not_called()
        self.resolve.assert_called_once_with(path)

    def test_data_only_reparse_point_is_an_ordinary_entry(self):
        # A OneDrive cloud placeholder (IO_REPARSE_TAG_CLOUD_6) does not redirect the path.
        self.entries[self.link] = SimpleNamespace(
            st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            st_reparse_tag=0x9000601A)
        self.resolve.side_effect = None
        self.resolve.return_value = self.path
        self.assertEqual(absolute(str(self.path)), str(self.path))
        self.readlink.assert_not_called()

    def test_unknown_name_surrogate_is_refused(self):
        self.entries[self.link] = SimpleNamespace(
            st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            st_reparse_tag=0xA0001234)
        self.assert_refused("^link target could not be inspected$")
        self.readlink.assert_not_called()

    def test_link_classifier_shared_with_git(self):
        # git_readonly._file uses the same classifier for repository components.
        cloud = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                                st_reparse_tag=0x9000601A)
        self.assertFalse(file_reads._is_redirecting_link(cloud, ReadBoundaryError))
        self.assertTrue(file_reads._is_redirecting_link(self.junction, ReadBoundaryError))
        surrogate = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                                    st_reparse_tag=0xA0001234)
        with self.assertRaisesRegex(ReadBoundaryError, "^link target could not be inspected$"):
            file_reads._is_redirecting_link(surrogate, ReadBoundaryError)

    def test_missing_tail_stops_walk_before_resolve(self):
        def inspect(path):
            if path == self.link:
                raise FileNotFoundError()
            if self.link in path.parents:
                self.fail("walk continued below missing component")
            return self.folder

        self.lstat.side_effect = inspect
        self.resolve.side_effect = None
        self.resolve.return_value = self.path
        self.assertEqual(absolute(str(self.path)), str(self.path))
        self.resolve.assert_called_once_with(self.path)
        self.readlink.assert_not_called()
        self.drive_type.assert_not_called()
