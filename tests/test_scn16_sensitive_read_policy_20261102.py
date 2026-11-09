"""SCN16 T1: never expose synthetic secret-looking paths via reads or search."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.file_reads import (
    ReadBoundaryError, _read_name_denied, _sensitive_component, _READ_DENY_SECRET_SUFFIXES, read_text_file,
    search_code, read_text_chunk, find_files, list_folder,
    list_folder_page, read_text_page, path_info, read_text_lines,
)
from tests._inherited_test_temp import inherited_test_temp


class SensitiveNamePolicyTests(unittest.TestCase):
    def test_names_casefolded(self):
        for name in ("start-sora-commander.cmd", "START-SORA-COMMANDER.CMD",
                     "secret.user.txt", "SECRET.USER.TXT", "runtime", "logs",
                     "state", "temp", "prod.env", "PROD.ENV"):
            with self.subTest(name=name):
                self.assertTrue(_read_name_denied(name))

    def test_environment_like_normal_names_are_not_denied(self):
        for name in ("environment.txt", "env.py"):
            with self.subTest(name=name):
                self.assertFalse(_read_name_denied(name))

    def test_env_suffix_does_not_expand_root_or_ancestor_secret_policy(self):
        self.assertNotIn(".env", _READ_DENY_SECRET_SUFFIXES)
        for name in ("prod.env", "PROD.ENV"):
            with self.subTest(name=name):
                self.assertFalse(_sensitive_component(name))

    def test_shared_dev_and_config_path_checks_reject_env_names_before_io(self):
        from unittest.mock import patch
        from sora_commander_next import allowed_roots, dev_rdc_compat
        for name in ("prod.env", "PROD.ENV"):
            path = "C:/Work/" + name
            with self.subTest(name=name), self.assertRaises(ReadBoundaryError):
                dev_rdc_compat._path(path)
            with patch.object(allowed_roots, "_plain_path") as filesystem, \
                    self.assertRaises(allowed_roots.AllowedRootsError):
                allowed_roots._validate_root(path, 1, Path("C:/Users/Sample"), (), ())
            filesystem.assert_not_called()

    def test_dummy_secret_is_inaccessible_by_any_read_route(self):
        with inherited_test_temp() as directory:
            root = Path(directory)
            fake = root / "start-sora-commander.cmd"
            fake.write_text("CONTROL_PLANE_API_KEY=DUMMY_NOT_A_REAL_KEY\n", encoding="utf-8")
            roots = [str(root)]
            with self.assertRaises(ReadBoundaryError):
                read_text_file(str(fake), roots)
            with self.assertRaises(ReadBoundaryError):
                read_text_chunk(str(fake), roots)
            with self.assertRaises(ReadBoundaryError):
                search_code(str(fake), roots, text="CONTROL_PLANE_API_KEY=")
            with self.assertRaises(ReadBoundaryError):
                find_files(str(fake), roots, name="*.cmd")
            with self.assertRaises(ReadBoundaryError):
                list_folder(str(fake), roots)
            for method in (read_text_page, read_text_lines, path_info,
                           list_folder_page):
                with self.subTest(method=method.__name__):
                    with self.assertRaises(ReadBoundaryError):
                        method(str(fake), roots)

    def test_root_cannot_grant_sensitive_directory(self):
        with inherited_test_temp() as directory:
            root = Path(directory)
            private = root / "runtime"
            private.mkdir()
            (private / "fake.txt").write_text("synthetic")
            with self.assertRaises(ReadBoundaryError):
                read_text_file(str(private / "fake.txt"), [str(private)])


if __name__ == "__main__":
    unittest.main()
