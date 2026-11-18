"""Offline path boundary regression tests."""
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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
                     SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)):
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
