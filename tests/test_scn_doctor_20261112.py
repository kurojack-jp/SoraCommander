"""Synthetic setup checks; no real Owner configuration, windows or connections."""
import builtins
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests._inherited_test_temp import inherited_test_temp

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("scn_doctor", ROOT / "tools/scn_doctor.py")
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(inherited_test_temp())).resolve()
        self.repo = self.folder / "copy"
        self.repo.mkdir()
        (self.repo / "pyproject.toml").write_text('requires-python = ">=3.13"\n', encoding="utf-8")
        self.home = self.folder / "Owner"
        self.config = self.home / ".config/sora-commander"
        self.config.mkdir(parents=True)
        self.env = {"USERPROFILE": str(self.home), "LOCALAPPDATA": str(self.home / "AppData/Local")}
        self.enterContext(patch.dict("os.environ", self.env, clear=True))
        # Synthetic scratch necessarily contains temp; keep all other real
        # root validation, including the actual loader and configuration IO.
        from sora_commander_next import allowed_roots
        denied = allowed_roots._read_name_denied
        self.enterContext(patch.object(allowed_roots, "_read_name_denied",
                                      side_effect=lambda p: False if p.casefold() in ("temp", "tmp") else denied(p)))

    def write(self, name, content):
        (self.config / name).write_text(content, encoding="utf-8")

    def call(self, check, **kwargs):
        return check(self.env, self.repo, **kwargs)

    def test_runtime_pass_and_each_hold(self):
        self.assertEqual(self.call(doctor.check_runtime, platform="win32", version=(3, 13))[0], "PASS")
        for platform, version in (("linux", (3, 13)), ("win32", (3, 12))):
            self.assertEqual(self.call(doctor.check_runtime, platform=platform, version=version)[0], "HOLD")
        (self.repo / "pyproject.toml").write_text("", encoding="utf-8")
        self.assertEqual(self.call(doctor.check_runtime)[0], "HOLD")

    def test_packages_each_import_failure(self):
        importer = Mock()
        self.assertEqual(self.call(doctor.check_packages, importer=importer)[0], "PASS")
        for missing in ("sora_commander_next", "win32api", "mcp"):
            def load(name):
                if name == missing:
                    raise ImportError("private detail must not be printed")
            self.assertEqual(self.call(doctor.check_packages, importer=load), ("HOLD", "packages", missing))

    def test_location_pass_and_warn(self):
        base = Path(self.repo.anchor) / "Sample" / "copy"
        self.assertEqual(doctor.check_location(self.env, base)[0], "PASS")
        for name in ("temp", "TMP", ".hidden"):
            self.assertEqual(doctor.check_location(self.env, base / name / "copy")[0], "WARN")

    def test_roots_missing_pass_invalid_empty_and_missing_folder(self):
        self.assertEqual(self.call(doctor.check_roots), ("WARN", "roots", "default"))
        self.write("allowed-roots.txt", str(self.repo))
        self.assertEqual(self.call(doctor.check_roots)[0], "PASS")
        for value in ("relative", "# empty\n", str(self.folder / "absent")):
            self.write("allowed-roots.txt", value)
            result = self.call(doctor.check_roots)
            self.assertEqual(result[0], "HOLD")
            self.assertTrue(result[2].startswith("allowed-roots line "))

    def test_roots_disappears_after_loading_warns(self):
        from sora_commander_next.allowed_roots import AllowedRoots
        self.write("allowed-roots.txt", str(self.repo))
        with patch("sora_commander_next.allowed_roots.startup_allowed_roots",
                   return_value=AllowedRoots((str(self.folder / "gone"),))) as loader:
            self.assertEqual(self.call(doctor.check_roots)[0], "WARN")
        loader.assert_called_once_with(self.repo, "development", self.env)

    def test_language_pass_fallback_invalid_and_error_verbatim(self):
        with patch("sora_commander_next.owner_ui_language._windows_ui_language", return_value="en"):
            self.assertEqual(self.call(doctor.check_language)[0], "PASS")
        self.write("language.txt", "ja")
        self.assertEqual(self.call(doctor.check_language), ("PASS", "language", "ja (file)"))
        self.write("language.txt", "invalid")
        self.assertEqual(self.call(doctor.check_language),
                         ("HOLD", "language", "language.txt must hold exactly one line: en or ja"))

    def test_tasks_off_valid_and_invalid(self):
        self.assertEqual(self.call(doctor.check_tasks), ("PASS", "tasks", "off"))
        task = dict(name="sample", description="Synthetic", cwd=str(self.repo),
                    executable=str(self.repo / "python.exe"), argv=[], timeout=10, output_limit=1024)
        self.write("tasks.json", json.dumps({"tasks": [task]}))
        self.assertEqual(self.call(doctor.check_tasks), ("PASS", "tasks", "ok"))
        for value in ("{}", '{"tasks": []}', "not json"):
            self.write("tasks.json", value)
            self.assertEqual(self.call(doctor.check_tasks)[0], "HOLD")

    def test_bridge_off_valid_invalid_and_no_startup(self):
        self.assertEqual(self.call(doctor.check_bridge), ("PASS", "bridge", "off"))
        self.write("ogf-bridge.json", json.dumps({"inbox": str(self.folder / "external-brain/inbox.json"),
                                                 "profile_id": "sample"}))
        with patch("sora_commander_next.ogf_cue.OgfBridge.startup", side_effect=AssertionError("startup forbidden")):
            self.assertEqual(self.call(doctor.check_bridge), ("PASS", "bridge", "ok"))
        self.write("ogf-bridge.json", "{}")
        self.assertEqual(self.call(doctor.check_bridge),
                         ("HOLD", "bridge", "invalid or unreadable configuration"))

    def test_tunnel_pass_missing_directory_link_and_never_read(self):
        paths = doctor.tunnel_paths(self.env, self.repo)
        self.assertEqual(self.call(doctor.check_tunnel)[0], "WARN")
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic - must never be read")
        with patch.object(Path, "open", side_effect=AssertionError("no content reads")), \
                patch.object(builtins, "open", side_effect=AssertionError("no content reads")):
            self.assertEqual(self.call(doctor.check_tunnel), ("PASS", "tunnel", "ok"))
            self.assertEqual(self.call(doctor.check_tunnel, paths=(paths[0].parent,))[0], "WARN")
        real = Path.lstat
        def linked(path, *args, **kwargs):
            if path == paths[1]:
                return SimpleNamespace(st_mode=0o100600, st_nlink=1, st_file_attributes=0x400)
            return real(path, *args, **kwargs)
        with patch.object(Path, "lstat", linked):
            self.assertEqual(self.call(doctor.check_tunnel)[0], "WARN")

    def test_broker_present_absent_unavailable(self):
        for exists, level, detail in ((True, "PASS", "present"), (False, "WARN", "absent")):
            probe = Mock(return_value=exists)
            self.assertEqual(self.call(doctor.check_broker, exists=probe), (level, "broker", detail))
            probe.assert_called_once_with()
        self.assertEqual(self.call(doctor.check_broker, exists=Mock(side_effect=OSError())),
                         ("WARN", "broker", "unavailable"))

    def test_pipe_reuses_name_and_only_lists_the_namespace(self):
        identity = SimpleNamespace(sid="synthetic", session_id=7)
        pipe = "\\\\.\\pipe\\Synthetic-Broker-7"
        with patch("sora_commander_next.windows_owner.current_windows_owner", return_value=identity), \
                patch("sora_commander_next.owner_broker_pipe.broker_pipe_name", return_value=pipe) as name, \
                patch.object(doctor.os, "listdir", return_value=["other", "synthetic-broker-7"]) as listing, \
                patch.object(doctor.os, "stat", side_effect=AssertionError("stat would connect")), \
                patch("builtins.open", side_effect=AssertionError("open would connect")), \
                patch("sora_commander_next.owner_broker_pipe.OwnerBrokerPipeClient", side_effect=AssertionError("no connection")):
            self.assertTrue(doctor.pipe_exists())
            name.assert_called_once_with("synthetic", 7)
            listing.assert_called_once_with("\\\\.\\pipe\\")
            listing.return_value = ["other", "Synthetic-Broker-70"]
            self.assertFalse(doctor.pipe_exists())

    def test_fix_hint_only_when_not_pass(self):
        results = iter((("PASS", "tasks", "ok"), ("WARN", "tunnel", "absent"), ("HOLD", "roots", "x")))
        checks = tuple((lambda env, root: next(results)) for _ in range(3))
        with patch.object(doctor, "CHECKS", checks), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(doctor.main(["--lang", "en"], environ=self.env, root=self.repo), 1)
        passed, warned, held = output.getvalue().splitlines()
        self.assertEqual(passed, "[PASS] tasks.json: OK")
        self.assertIn(doctor.MESSAGES["en"]["tunnel_fix"], warned)
        self.assertIn(doctor.MESSAGES["en"]["roots_fix"], held)
        for function in doctor.CHECKS:
            key = function.__name__.removeprefix("check_")
            self.assertIn(key, doctor.MESSAGES["en"])
            self.assertIn(key + "_fix", doctor.MESSAGES["en"])

    def snapshot(self):
        return {str(p.relative_to(self.folder)): (p.stat().st_size, p.stat().st_mtime_ns)
                for p in self.folder.rglob("*")}

    def test_main_read_only_nine_lines_and_warn_exit_zero(self):
        self.write("allowed-roots.txt", str(self.repo))
        paths = doctor.tunnel_paths(self.env, self.repo)
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic")
        before = self.snapshot()
        real_open = Path.open
        def guarded(path, *args, **kwargs):
            self.assertNotIn(path, paths[1:])
            mode = args[0] if args else kwargs.get("mode", "r")
            self.assertFalse(any(c in mode for c in "wax+"))
            return real_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded), \
                patch.object(doctor, "pipe_exists", side_effect=AssertionError("default probe must be replaced")), \
                patch.object(doctor, "CHECKS", doctor.CHECKS[:-1] + (lambda env, root: doctor.check_broker(env, root, exists=lambda: False),)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(doctor.main(["--lang", "en"], environ=self.env, root=self.repo), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 9)
        self.assertIn("[WARN]", output.getvalue())
        self.assertEqual(before, self.snapshot())

    def test_main_hold_and_lang_override_even_invalid_language(self):
        for language, heading in (("en", "Packages"), ("ja", "パッケージ")):
            self.write("language.txt", "ja" if language == "en" else "en")
            with patch.object(doctor, "CHECKS", (lambda env, root: ("HOLD", "packages", "mcp"),)), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(doctor.main(["--lang", language], environ=self.env, root=self.repo), 1)
            self.assertIn(heading, output.getvalue())
        self.write("language.txt", "invalid")
        with patch.object(doctor, "CHECKS", (doctor.check_language,)), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(doctor.main(["--lang", "ja"], environ=self.env, root=self.repo), 1)
        self.assertIn("language.txt must hold exactly one line", output.getvalue())

    def test_both_languages_complete_and_english_fallback(self):
        self.assertEqual(set(doctor.MESSAGES["en"]), set(doctor.MESSAGES["ja"]))
        for lang in ("en", "ja"):
            self.assertTrue(all(doctor.MESSAGES[lang].values()))
        with patch.dict(doctor.MESSAGES, {"partial": {}}):
            self.assertEqual(doctor.text("broker", "partial"), doctor.MESSAGES["en"]["broker"])

    @unittest.skipUnless((ROOT / "tools/build_public_tree.py").is_file(),
                         "the public tree has no builder; this checks the development checkout")
    def test_new_public_files_pass_unchanged_scan(self):
        spec = importlib.util.spec_from_file_location("doctor_public_scan", ROOT / "tools/build_public_tree.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        chosen = {name: (ROOT / name).read_bytes() for name in
                  ("tools/scn_doctor.py", "tests/test_scn_doctor_20261112.py")}
        self.assertEqual(builder.scan(chosen), [])
        self.assertIn("tools/scn_doctor.py", builder.TOOLS)
        self.assertIn("tests/test_scn_doctor_20261112.py",
                      (ROOT / "docs/public_release/tests.txt").read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
