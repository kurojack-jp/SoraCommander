"""Synthetic setup checks with filesystem fixtures and no child processes."""

from contextlib import ExitStack, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from tests._inherited_test_temp import inherited_test_temp
from tools import scn_setup


class SetupHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(cls.enterClassContext(inherited_test_temp()))

    def setUp(self):
        case = self.scratch / self._testMethodName
        self.root = case / "checkout"
        self.profile = case / "profile"
        self.root.mkdir(parents=True)
        self.profile.mkdir()
        self.tunnel = self.profile / ".config/tunnel-client"
        self.yaml = self.tunnel / "sora-next.yaml"
        self.key = self.tunnel / "sora-next.key"
        self.allowed = self.profile / ".config/sora-commander/allowed-roots.txt"
        self.python = self.root / ".venv/Scripts/python.exe"
        self.template = self.root / "docs/sora-next.example.yaml"
        self.template.parent.mkdir()
        self.template.write_bytes(b"# synthetic template\n")
        self.env = {
            **os.environ,
            "USERPROFILE": str(self.profile),
            "LOCALAPPDATA": str(self.profile / "local"),
            "PIP_EXTRA_INDEX_URL": "unused",
        }
        self.package_status = 0
        self.version_status = 0
        self.doctor_status = 0
        self.calls = self.enterContext(patch.object(
            scn_setup.subprocess, "run", side_effect=self.fake_run,
        ))
        self.git = self.enterContext(patch.object(scn_setup.shutil, "which", return_value="git"))

    def fake_run(self, argv, **kwargs):
        self.assertEqual(kwargs["cwd"], self.root)
        if argv[0] == "py":
            self.assertEqual(argv[1:], ["-3.13", "-I", "-B", "-m", "venv", ".venv"])
            self.create_venv()
            code = 0
        else:
            self.assertEqual(argv[:3], [str(self.python), "-I", "-B"])
            if argv[3:5] == ["-c", scn_setup.VERSION_PROBE]:
                code = self.version_status
            elif argv[3:5] == ["-c", scn_setup.PACKAGE_PROBE]:
                self.assertEqual(tuple(argv[5:]), scn_setup.REQUIREMENTS)
                code = self.package_status
            elif argv[3:5] == ["-m", "pip"]:
                self.assertIn("https://pypi.org/simple", argv)
                self.assertEqual(tuple(argv[-3:]), ("mcp>=1,<2", "pydantic>=2,<3", "pywin32>=306"))
                self.assertEqual(kwargs["env"]["PIP_CONFIG_FILE"], os.devnull)
                self.assertNotIn("PIP_EXTRA_INDEX_URL", kwargs["env"])
                code = 0
            else:
                self.assertEqual(argv[-1], str(self.root / "tools/scn_doctor.py"))
                self.assertEqual(kwargs["env"], self.env)
                code = self.doctor_status
        return subprocess.CompletedProcess(argv, code)

    def create_venv(self):
        self.python.parent.mkdir(parents=True, exist_ok=True)
        self.python.write_bytes(b"synthetic python")

    def run_setup(self, *options, root=None):
        output = io.StringIO()
        with redirect_stdout(output):
            code = scn_setup.main(options, root=root or self.root, environ=self.env)
        return code, output.getvalue()

    def test_check_missing_files_changes_nothing(self):
        code, output = self.run_setup("--check")
        self.assertEqual(code, 0)
        self.assertIn("[TODO] Create the Python 3.13 .venv.", output)
        self.assertIn("[TODO] Copy sora-next.example.yaml", output)
        self.assertEqual(list(self.root.iterdir()), [self.template.parent])
        self.assertEqual(list(self.template.parent.iterdir()), [self.template])
        self.assertEqual(list(self.profile.iterdir()), [])
        self.calls.assert_not_called()

    def test_check_with_venv_does_not_install_or_copy(self):
        self.create_venv()
        self.package_status = 1
        code, output = self.run_setup("--check")
        self.assertEqual(code, 0)
        self.assertIn("[TODO] Install or update the packages", output)
        self.assertEqual(self.calls.call_count, 3)
        self.assertFalse(self.tunnel.exists())

    def test_preserves_existing_venv_yaml_and_allowed_folders(self):
        self.create_venv()
        self.tunnel.mkdir(parents=True)
        self.allowed.parent.mkdir()
        self.yaml.write_bytes(b"synthetic yaml")
        self.allowed.write_bytes(b"synthetic allowed roots")
        code, output = self.run_setup()
        self.assertEqual(code, 0)
        self.assertIn("[OK] Existing .venv uses Python 3.13.", output)
        self.assertIn("[OK] The packages from INSTALL section 1 are satisfied.", output)
        self.assertEqual(self.calls.call_count, 3)
        self.assertEqual(self.python.read_bytes(), b"synthetic python")
        self.assertEqual(self.yaml.read_bytes(), b"synthetic yaml")
        self.assertEqual(self.allowed.read_bytes(), b"synthetic allowed roots")

    def test_creates_venv_and_yaml_installs_only_published_requirements(self):
        self.package_status = 1
        code, output = self.run_setup()
        self.assertEqual(code, 0)
        self.assertIn("[DONE] Created Python 3.13 .venv.", output)
        self.assertIn("[DONE] Installed the packages", output)
        self.assertIn("[DONE] Copied sora-next.example.yaml", output)
        self.assertEqual(self.yaml.read_bytes(), self.template.read_bytes())
        self.assertFalse(self.allowed.exists())
        self.assertEqual(self.calls.call_count, 5)
        self.calls.reset_mock()
        self.package_status = 0
        self.assertEqual(self.run_setup()[0], 0)
        self.assertEqual(self.calls.call_count, 3)

    def test_missing_tunnel_client_and_key_yield_manual_steps(self):
        self.create_venv()
        code, output = self.run_setup()
        self.assertEqual(code, 0)
        self.assertIn(f"[TODO] {scn_setup.INSTALL_HINT}", output)
        self.assertIn(scn_setup.TUNNEL_GUIDE, output)
        self.assertIn(scn_setup.KEY_COMMAND, output)
        self.assertFalse((self.root / "tools/tunnel-client").exists())
        self.assertFalse(self.key.exists())
        self.assertEqual(self.calls.call_count, 3)

    def test_copies_example_from_source_checkout_layout(self):
        self.create_venv()
        source = self.root / "docs/public_release" / self.template.name
        source.parent.mkdir(parents=True)
        self.template.rename(source)
        code, output = self.run_setup()
        self.assertEqual(code, 0)
        self.assertIn("[DONE] Copied sora-next.example.yaml", output)
        self.assertEqual(self.yaml.read_bytes(), source.read_bytes())

    def test_failed_copy_leaves_no_partial_yaml(self):
        self.create_venv()
        with patch.object(scn_setup.os, "fsync", side_effect=OSError("synthetic write failure")):
            code, output = self.run_setup()
        self.assertEqual(code, 2)
        self.assertIn("[HOLD] synthetic write failure", output)
        self.assertFalse(self.yaml.exists())
        self.assertEqual(list(self.tunnel.iterdir()), [])

    def test_existing_key_and_yaml_are_never_opened(self):
        self.create_venv()
        self.tunnel.mkdir(parents=True)
        self.key.touch()
        self.yaml.touch()
        with ExitStack() as stack:
            for target in ("builtins.open", "io.open"):
                stack.enter_context(patch(target, side_effect=AssertionError("unexpected file open")))
            code, output = self.run_setup()
        self.assertEqual(code, 0)
        self.assertIn("[OK] The tunnel key file is present.", output)
        self.assertIn("[OK] sora-next.yaml already exists.", output)

    def test_space_in_path_exits_six(self):
        code, output = self.run_setup("--check", root=self.root / "with space")
        self.assertEqual(code, 6)
        self.assertIn("[HOLD] The folder path contains a space.", output)
        self.calls.assert_not_called()

    def test_unsafe_path_exits_six(self):
        code, output = self.run_setup("--check", root=self.root / "checkout^")
        self.assertEqual(code, 6)
        self.assertIn("[HOLD] The folder path contains an unsupported character.", output)
        self.calls.assert_not_called()

    def test_muse_only_skips_all_tunnel_steps_including_doctor_check(self):
        self.create_venv()
        with patch.object(scn_setup, "report_tunnel", side_effect=AssertionError("tunnel check")):
            code, output = self.run_setup("--muse-only")
        self.assertEqual(code, 0)
        self.assertIn(r"tools\start-muse-bridge-tailscale.cmd", output)
        self.assertIn("tcp:8765", output)
        self.assertNotIn("ChatGPT", output)
        self.assertFalse(self.tunnel.exists())
        self.assertEqual(self.calls.call_args.args[0][3:5], ["-c", scn_setup.MUSE_DOCTOR])

    def test_wrong_existing_venv_holds_without_recreate(self):
        self.create_venv()
        self.version_status = 1
        code, output = self.run_setup()
        self.assertEqual(code, 2)
        self.assertIn("[HOLD] .venv does not provide", output)
        self.calls.assert_called_once()
        self.assertEqual(self.python.read_bytes(), b"synthetic python")

    def test_doctor_failure_maps_to_hold(self):
        self.create_venv()
        self.doctor_status = 1
        code, output = self.run_setup("--check")
        self.assertEqual(code, 2)
        self.assertIn("[HOLD] Setup doctor reported a problem.", output)

    def test_missing_git_holds(self):
        self.git.return_value = None
        code, output = self.run_setup()
        self.assertEqual(code, 2)
        self.assertIn("[HOLD] Install Git", output)
        self.calls.assert_not_called()

    def test_non_ascii_unverified_and_explicitly_verified_are_both_allowed(self):
        root = self.root / "\u65e5\u672c\u8a9e"
        docs = root / "docs"
        docs.mkdir(parents=True)
        report = docs / "SCN_NON_ASCII_PATHS_RESULT_2026-11-20.md"
        report.write_text("- Live launchers are unverified.\n", encoding="utf-8")
        code, output = self.run_setup("--check", root=root)
        self.assertEqual(code, 0)
        self.assertIn("[TODO] Caution: live launchers on non-ASCII checkout paths are unverified.", output)
        report.write_text("- Live launchers work on non-ASCII checkout paths.\n", encoding="utf-8")
        code, output = self.run_setup("--check", root=root)
        self.assertEqual(code, 0)
        self.assertIn("[OK] Live launchers are verified for non-ASCII checkout paths.", output)
