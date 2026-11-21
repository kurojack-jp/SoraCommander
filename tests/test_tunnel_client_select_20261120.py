"""Synthetic checkout tests for tunnel-client version selection."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import venv

from tests._inherited_test_temp import inherited_test_temp
from tools import scn_tunnel_client as selector


class LauncherTests(unittest.TestCase):
    launchers = tuple(f"start-sora-commander-{kind}.cmd"
                      for kind in ("broker-dev", "local"))
    source = Path(selector.__file__).parent

    def test_shared_rule_rejects_all_disallowed_characters(self):
        for char in '&%^!()"<>|\x01\x7f ':
            with self.subTest(char=char):
                self.assertIsNotNone(selector.checkout_path_hold(Path(f"check{char}out")))
        self.assertIsNone(selector.checkout_path_hold(Path("合成フォルダ")))

    def available_launchers(self):
        available = []
        for name in self.launchers:
            with self.subTest(launcher=name):
                available.append(name)
        return available

    def fixture(self, name, files, *, with_venv=False):
        scratch = Path(self.enterContext(inherited_test_temp()))
        root = scratch / name
        tools = root / "tools"
        tools.mkdir(parents=True)
        for filename in files:
            shutil.copyfile(self.source / filename, tools / filename)
        if with_venv:
            venv.EnvBuilder().create(root / ".venv")
        env = {**os.environ, "USERPROFILE": str(scratch), "LOCALAPPDATA": str(scratch),
               "CONTROL_PLANE_API_KEY": "synthetic"}
        return tools, env

    def invoke(self, tools, env, name):
        return subprocess.run(
            ["cmd.exe", "/d", "/c", rf".\{name}", "--check"], cwd=tools, env=env,
            stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30,
        )

    def test_three_batch_launchers_refuse_special_checkout_before_key_check(self):
        launchers = self.available_launchers()
        tools, env = self.fixture("unsafe&%PATH%^!()", (*launchers, "scn_tunnel_client.py"),
                                  with_venv=True)
        for name in launchers:
            with self.subTest(launcher=name):
                result = self.invoke(tools, env, name)
                self.assertEqual(result.returncode, 6, result.stdout + result.stderr)
                self.assertEqual(len(result.stdout.splitlines()), 1)
                self.assertIn("unsupported character", result.stdout)

    def test_setup_and_update_batch_wrappers_refuse_unsafe_checkout(self):
        tools, env = self.fixture(
            "unsafe&%PATH%^!()", ("setup.cmd", "update.cmd", "scn_setup.py", "scn_update.py",
                                  "scn_tunnel_client.py", "scn_broker_autostart.py",
                                  "scn_muse_autostart.py"))
        for name in ("setup.cmd", "update.cmd"):
            with self.subTest(wrapper=name):
                result = self.invoke(tools, env, name)
                self.assertEqual(result.returncode, 6, result.stdout + result.stderr)
                self.assertEqual(len(result.stdout.splitlines()), 1)
                self.assertIn("unsupported character", result.stdout)

    def test_three_batch_launchers_check_complete_selector_result(self):
        launchers = self.available_launchers()
        tools, env = self.fixture("checkout", (*launchers, "scn_tunnel_client.py",
                                               "scn_launch_check.py"), with_venv=True)
        (tools / "scn_tunnel_key.py").write_text("raise SystemExit(0)\n", encoding="ascii")
        selector_copy = tools / "scn_tunnel_client.py"
        script = selector_copy.read_text(encoding="utf-8")
        path = r"tools\tunnel-client\v0.1.0\tunnel-client.exe"
        executable = tools.parent / path
        executable.parent.mkdir(parents=True)
        executable.touch()
        for output, code, expected in (
            (path, 0, "sora-next.yaml"),
            (path + "\n" + path, 0, "Could not validate"),
            (path + "\n", 0, "Could not validate"),
            (path, 7, "Could not validate"),
            (r"tools\elsewhere\tunnel-client.exe", 0, "Could not validate"),
        ):
            selector_copy.write_text(script.replace(
                "raise SystemExit(main(checkout))", f"print({output!r})\n    raise SystemExit({code})",
            ), encoding="utf-8")
            for name in launchers:
                with self.subTest(launcher=name, output=output, exit_code=code):
                    result = self.invoke(tools, env, name)
                    combined = result.stdout + result.stderr
                    self.assertEqual(result.returncode, 2, combined)
                    self.assertIn(expected, combined)


class TunnelClientSelectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(inherited_test_temp()))
        self.folder = self.root / selector.TUNNEL_CLIENT_DIR

    def install(self, version):
        executable = self.folder / version / "tunnel-client.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"synthetic")
        return executable

    def test_highest_version_is_numeric(self):
        self.install("v0.0.9")
        middle = self.install("v0.0.16")
        self.assertEqual(selector.select_tunnel_client(self.root), middle)
        newest = self.install("v0.1.0")
        self.assertEqual(selector.select_tunnel_client(self.root), newest)

    def test_invalid_names_and_incomplete_folders_are_ignored(self):
        valid = self.install("v0.0.9")
        for name in ("v1", "latest", "v0.0.16-beta"):
            self.install(name)
        (self.folder / "v1.0.0").mkdir()
        (self.folder / "v2.0.0" / "tunnel-client.exe").mkdir(parents=True)
        self.assertEqual(selector.select_tunnel_client(self.root), valid)

    def test_reparse_directories_and_symlink_executables_are_refused(self):
        valid = self.install("v0.0.9")
        linked_executable = self.install("v1.0.0")
        linked_version = self.install("v2.0.0").parent
        lstat = Path.lstat
        junction = SimpleNamespace(
            st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
        symlink = SimpleNamespace(st_mode=stat.S_IFLNK)

        def mock_lstat(path):
            if linked_version in path.parents:
                self.fail("must not inspect a junction's children")
            if path == linked_version:
                return junction
            if path == linked_executable:
                return symlink
            return lstat(path)

        with patch.object(Path, "lstat", mock_lstat):
            self.assertEqual(selector.select_tunnel_client(self.root), valid)

    def test_reparse_tunnel_root_is_not_traversed(self):
        self.install("v0.0.9")
        lstat = Path.lstat

        def mock_lstat(path):
            if path == self.folder:
                return SimpleNamespace(
                    st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
                )
            return lstat(path)

        with patch.object(Path, "lstat", mock_lstat), patch.object(
            Path, "iterdir", side_effect=AssertionError("must not enumerate the junction")
        ):
            self.assertIsNone(selector.select_tunnel_client(self.root))

    def test_missing_checkout_client_holds_without_consulting_path(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "tunnel-client.exe").write_bytes(b"synthetic")
        output, diagnostics = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {"PATH": str(outside)}), patch(
            "shutil.which", side_effect=AssertionError("must not search PATH")
        ), redirect_stdout(output), redirect_stderr(diagnostics):
            self.assertEqual(selector.main(self.root), 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(diagnostics.getvalue().splitlines(), [f"[HOLD] {selector.INSTALL_HINT}"])

    def test_cli_reports_the_version_once_and_emits_only_the_relative_path(self):
        executable = self.install("v0.1.0")
        output, diagnostics = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostics):
            self.assertEqual(selector.main(self.root), 0)
        self.assertEqual(output.getvalue().splitlines(), [str(executable.relative_to(self.root))])
        self.assertEqual(diagnostics.getvalue().splitlines(), ["[INFO] Selected tunnel-client v0.1.0."])
