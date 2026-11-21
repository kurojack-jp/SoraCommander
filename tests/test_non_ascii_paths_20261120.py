"""Windows synthetic Unicode paths through config, MCP, Git and doctor."""

import asyncio
from codecs import BOM_UTF8
from contextlib import ExitStack, redirect_stdout
from functools import partial
from hashlib import sha256
import io
import os
from pathlib import Path, PureWindowsPath
import stat
import subprocess
from types import SimpleNamespace
import unicodedata
import unittest
from unittest.mock import patch

from sora_commander_next import allowed_roots, file_reads, git_readonly
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import build_server
from sora_commander_next.policy import Policy
from tests._inherited_test_temp import inherited_test_temp
from tools import scn_doctor


class NonAsciiRefusalTests(unittest.TestCase):
    def test_protected_and_dos_device_names(self):
        for form in ("NFC", "NFD"):
            with self.subTest(form=form):
                stem = unicodedata.normalize(form, "ガイドcafé")
                self.assertFalse(file_reads._read_name_denied(stem + ".md"))
                for name in (stem + ".pem", "token." + stem, "NUL." + stem):
                    self.assertTrue(file_reads._read_name_denied(name), name)
                path = str(PureWindowsPath("D:/", "資料", stem + ".pem"))
                with patch.object(Path, "lstat", side_effect=AssertionError("protected path inspected")):
                    with self.assertRaisesRegex(file_reads.ReadBoundaryError, "sensitive path excluded"):
                        file_reads._check(path, (str(PureWindowsPath("D:/", "資料")),))

    def test_unc_and_device_path_refusal_before_io(self):
        for form in ("NFC", "NFD"):
            name = unicodedata.normalize(form, "ガイドcafé")
            for prefix in ("\\\\synthetic\\資料\\", "\\\\?\\D:\\資料\\", "\\\\.\\D:\\資料\\"):
                with self.subTest(form=form, prefix=prefix):
                    path = prefix + name
                    with patch.object(Path, "lstat", side_effect=AssertionError("namespace inspected")):
                        with self.assertRaisesRegex(file_reads.ReadBoundaryError,
                                                    "UNC and device paths are unsupported"):
                            file_reads._check(path, ("unused",))
                        with self.assertRaisesRegex(ValueError, "absolute local path required"):
                            allowed_roots._absolute(path)

    def test_japanese_network_link_refused_before_traversal(self):
        root = Path("D:/")
        for form in ("NFC", "NFD"):
            link = root / unicodedata.normalize(form, "ガイドcafé")
            with self.subTest(form=form), \
                    patch.object(Path, "lstat", autospec=True,
                                 return_value=SimpleNamespace(st_mode=stat.S_IFLNK)) as lstat, \
                    patch.object(file_reads.os, "readlink",
                                 return_value="\\\\synthetic\\資料") as readlink:
                with self.assertRaisesRegex(file_reads.ReadBoundaryError, "link leads to a network location"):
                    file_reads._check(str(link / "日本語のメモ.md"), (str(root),))
                lstat.assert_called_once_with(link)
                readlink.assert_called_once_with(link)


class NonAsciiFilesystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(cls.enterClassContext(inherited_test_temp()))
        cls.checkout = cls.scratch / "資料"
        cls.folder = cls.checkout / "テスト"
        cls.read_only = cls.scratch / "café"
        cls.home = cls.scratch / "synthetic-profile"
        cls.local = cls.home / "AppData" / "Local"
        cls.env = {
            "USERPROFILE": str(cls.home),
            "LOCALAPPDATA": str(cls.local),
            "HOME": str(cls.home),
        }
        for directory in (cls.folder, cls.read_only, cls.local):
            directory.mkdir(parents=True)
        cls.enterClassContext(patch.dict(os.environ, cls.env))
        cls.note = cls.folder / "日本語のメモ.md"
        cls.other = cls.read_only / "café.md"
        cls.content = "最初の行\n検索対象の日本語\n"
        cls.note.write_bytes(cls.content.encode("utf-8"))
        cls.other.write_bytes("café\n".encode("utf-8"))
        cls.config = cls.home / ".config" / "sora-commander" / "allowed-roots.txt"
        cls.config.parent.mkdir(parents=True)
        cls.reference = cls.scratch / "参照"
        cls.reference.mkdir()
        cls.config_data = f"{cls.checkout}\nread:{cls.reference}\nread:{cls.read_only}\n"
        cls.config.write_bytes(cls.config_data.encode("utf-8"))

    def test_allowed_roots_utf8_with_and_without_bom(self):
        with self.temp_parent_admission():
            for prefix in (b"", BOM_UTF8):
                with self.subTest(bom=bool(prefix)):
                    data = prefix + self.config_data.encode("utf-8")
                    self.config.write_bytes(data)
                    for roots in (allowed_roots.parse_allowed_roots(data, self.env),
                                  allowed_roots.startup_allowed_roots(self.checkout, "development", self.env)):
                        self.assertEqual(roots.full, (str(self.checkout.resolve()),))
                        self.assertEqual(roots.read_only,
                                         (str(self.reference.resolve()), str(self.read_only.resolve())))
        with self.assertRaisesRegex(allowed_roots.AllowedRootsError, "allowed-roots line 2: invalid UTF-8"):
            allowed_roots.parse_allowed_roots(b"# UTF-8\n\xff", self.env)

    def temp_parent_admission(self):
        stack = ExitStack()
        # Allow the fixture's reserved ancestor name; retain every other name and path check.
        for module, attribute in ((allowed_roots, "_read_name_denied"),
                                  (file_reads, "_sensitive_component")):
            original = getattr(module, attribute)
            stack.enter_context(patch.object(module, attribute, side_effect=partial(
                self.check_fixture_name, original)))
        return stack

    def check_fixture_name(self, original, name):
        return name != self.scratch.parent.name and original(name)

    def server(self):
        host = Host(policy=Policy(roots=(str(self.checkout),),
                                  read_only_roots=(str(self.read_only),)))
        self.addCleanup(host.jobs.close)
        return build_server(host)

    @staticmethod
    def call(server, tool, **arguments):
        _, result = asyncio.run(server.call_tool(tool, arguments))
        return result

    def test_mcp_read_tools(self):
        with self.temp_parent_admission():
            server = self.server()
            note, folder, other = map(str, (self.note, self.folder, self.other))
            digest = sha256(self.content.encode("utf-8")).hexdigest()
            self.assertEqual(self.call(server, "read_text_file", path=note)["content"], self.content)
            self.assertEqual(self.call(server, "read_text_lines", path=note, start_line=2)["lines"],
                             [{"line": 2, "text": "検索対象の日本語\n"}])
            self.assertEqual(self.call(server, "list_folder", path=folder)["entries"],
                             [{"name": self.note.name, "type": "file"}])
            self.assertEqual(self.call(server, "find_files", path=folder, name="日本語")["matches"],
                             [{"path": self.note.name, "type": "file"}])
            self.assertEqual(self.call(server, "search_code", path=folder, text="検索対象")["matches"],
                             [{"path": self.note.name, "line": 2, "excerpt": "検索対象の日本語"}])
            info = self.call(server, "path_info", path=note)
            self.assertEqual((info["type"], info["size_bytes"], info["sha256"]),
                             ("file", len(self.content.encode("utf-8")), digest))
            self.assertEqual(self.call(server, "sha256_file", path=note)["sha256"], digest)
            results = self.call(server, "read_many_text_files", paths=[note, other])["files"]
            self.assertEqual([(row["path"], row["content"]) for row in results],
                             [(note, self.content), (other, "café\n")])

    def test_git_snapshot_and_log(self):
        repo = self.checkout / "履歴"
        repo.mkdir()
        self.addCleanup(self.make_git_objects_writable, repo / ".git" / "objects")
        exe = git_readonly.fixed_git()
        env = git_readonly.safe_env(exe)
        subject = "日本語のコミット"
        (repo / self.note.name).write_bytes(b"synthetic\n")
        for args in (("init", "-b", "synthetic"), ("add", "--", self.note.name),
                     ("-c", "user.name=Synthetic Author", "-c", "user.email=synthetic@example.invalid",
                      "-c", "commit.gpgSign=false", "commit", "-m", subject)):
            result = subprocess.run([str(exe), *git_readonly.OPTIONS, *args], cwd=repo, env=env,
                                    capture_output=True, timeout=15, check=False,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        with self.temp_parent_admission():
            server = self.server()
            snapshot = self.call(server, "git_snapshot", path=str(repo))
            self.assertIn("branch.head synthetic", snapshot["status"])
            self.assertIn(subject, snapshot["log"])
            log = self.call(server, "git_log", path=str(repo), paths=[self.note.name])
            self.assertIn(subject, log["content"])
            # Names come back as real UTF-8, not "\343\203..." octal escapes.
            shown = self.call(server, "git_show", path=str(repo), rev="HEAD")
            self.assertIn(self.note.name, shown["content"])
            self.assertNotIn("\\3", shown["content"])
            (repo / self.note.name).write_bytes(b"synthetic\nchanged\n")
            (repo / "未追跡の café.md").write_bytes(b"untracked\n")
            snapshot = self.call(server, "git_snapshot", path=str(repo), include_untracked=True)
            self.assertIn(self.note.name, snapshot["status"])
            self.assertEqual(snapshot["untracked"], ["未追跡の café.md"])
            diff = self.call(server, "git_diff", path=str(repo), stat_only=True)
            self.assertIn(self.note.name, diff["content"])
            self.assertNotIn("\\3", snapshot["status"] + diff["content"])

    @staticmethod
    def make_git_objects_writable(objects):
        for item in objects.rglob("*"):
            if item.is_file() and not item.is_symlink():
                item.chmod(0o666)

    def test_doctor_in_process(self):
        (self.checkout / "pyproject.toml").write_text(
            '[project]\nrequires-python = ">=3.13"\n', encoding="utf-8")
        checks = (*scn_doctor.CHECKS[:-3],
                  lambda env, root: ("PASS", "bridge", "off"),
                  partial(scn_doctor.check_tunnel, paths=(self.checkout / "synthetic-executable.exe",)),
                  partial(scn_doctor.check_broker, exists=lambda: False))
        output = io.StringIO()
        with self.temp_parent_admission(), patch.object(scn_doctor, "CHECKS", checks), redirect_stdout(output):
            self.assertEqual(scn_doctor.main(["--lang", "en"], environ=self.env, root=self.checkout), 0,
                             output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 9)
        self.assertIn("[PASS] " + scn_doctor.text("roots", "en"), output.getvalue())
        self.assertIn("[WARN] " + scn_doctor.text("location", "en"), output.getvalue())
