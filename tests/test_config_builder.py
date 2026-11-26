"""Config Builder contract tests. Synthetic settings only; never open a GUI."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import Mock, patch

from tests._inherited_test_temp import inherited_test_temp
from tools import scn_config_builder as entry
from config_builder import model

ROOT = Path("C:/BuilderExample")  # Virtual checkout, never an actual user project.
EXAMPLE = ROOT
ENV = {"USERPROFILE": "C:/BuilderProfile", "LOCALAPPDATA": "C:/BuilderProfile/AppData/Local",
       "SystemRoot": "C:/Windows", "SystemDrive": "C:"}


def task(**changes):
    value = {"name": "unit-test", "description": "Synthetic test",
             "cwd": str(EXAMPLE), "executable": "C:/Windows/System32/cmd.exe",
             "argv": ["/d", "/s", "/c", "npm.cmd test"],
             "timeout": 10, "output_limit": 8192}
    value.update(changes)
    return value


def encoded(tasks):
    return json.dumps({"tasks": tasks}, ensure_ascii=False).encode("utf-8")


@unittest.skipUnless(os.name == "nt", "Windows path contract")
class ValidationTests(unittest.TestCase):
    def roots(self, data):
        # Virtual root metadata only. Name, overlap and scope rules stay real.
        with patch.object(model.allowed_roots, "_plain_path", side_effect=lambda p, **kw: p):
            return model.allowed_roots.parse_allowed_roots(data, ENV)

    def test_roots_limit_bom_empty_and_case(self):
        rows = [f"read: C:/BuilderExample/p{index}" for index in range(16)]
        self.assertEqual(len(self.roots("\n".join(rows).encode()).read_only), 16)
        for value in ("\n".join(rows + ["read: C:/BuilderExample/overflow"]), "# empty\n", "READ: C:/BuilderExample"):
            with self.subTest(value=value[:24]), self.assertRaises(model.allowed_roots.AllowedRootsError):
                self.roots(value.encode())
        self.assertEqual(len(self.roots(b"\xef\xbb\xbfread: C:/BuilderExample").read_only), 1)

    def test_roots_overlap_and_same_mode_semantics(self):
        for value in ("C:/BuilderExample\nread: C:/BuilderExample",
                      "C:/BuilderExample\nread: C:/BuilderExample/sub",
                      "read: C:/BuilderExample\nC:/BuilderExample/sub"):
            with self.subTest(value=value), self.assertRaises(model.allowed_roots.AllowedRootsError):
                self.roots(value.encode())
        self.assertEqual(len(self.roots(b"C:/BuilderExample\nC:/BuilderExample/sub").full), 2)

    def test_roots_reject_unc_drive_private_and_invalid_names(self):
        paths = ("//server/share/work", "C:/", "C:/Windows/work", "C:/ProgramData/work",
                 "C:/Program Files/work", ENV["USERPROFILE"], ENV["LOCALAPPDATA"],
                 "C:/BuilderProfile/.config", "C:/BuilderExample/secrets",
                 "C:/BuilderExample/prod.env", "C:/BuilderExample/../other", '"C:/BuilderExample"')
        for path in paths:
            with self.subTest(path=path), self.assertRaises(model.allowed_roots.AllowedRootsError):
                self.roots(path.encode())

    def test_roots_byte_limit_and_invalid_encoding(self):
        base = b"C:/BuilderExample\n#"
        self.roots(base + b"x" * (65536 - len(base)))
        for data in (base + b"x" * (65537 - len(base)), b"\xff"):
            with self.subTest(size=len(data)), self.assertRaises(model.allowed_roots.AllowedRootsError):
                self.roots(data)

    def test_language_exact_limit_and_values(self):
        self.assertEqual(model.owner_ui_language._parse(b"ja\n#" + b"x" * 252), "ja")
        self.assertEqual(model.owner_ui_language._parse(b"\xef\xbb\xbfen\n"), "en")
        for data in (b"ja\n#" + b"x" * 253, b"JA", b"en\nja", b"# only", b"\xff"):
            with self.subTest(data=data[:12]), self.assertRaises(model.owner_ui_language.OwnerLanguageError):
                model.owner_ui_language._parse(data)

    def test_tasks_count_and_exact_byte_limit(self):
        data = encoded([task(name=f"t-{i}") for i in range(32)])
        self.assertEqual(len(model.task_presets.parse_task_presets(data)), 32)
        for rows in ([], [task(name=f"t-{i}") for i in range(33)], [task(), task()]):
            with self.assertRaises(model.task_presets.TaskPresetError):
                model.task_presets.parse_task_presets(encoded(rows))
        data = encoded([task()])
        model.task_presets.parse_task_presets(data + b" " * (65536 - len(data)))
        with self.assertRaises(model.task_presets.TaskPresetError):
            model.task_presets.parse_task_presets(data + b" " * (65537 - len(data)))

    def test_tasks_keys_duplicates_and_names(self):
        missing = task()
        del missing["cwd"]
        invalid = [encoded([missing]), encoded([task(extra=1)]), b'{"tasks":[],"tasks":[]}',
                   b'{"tasks":[],"other":0}', b'[]', b'\xff']
        invalid += [encoded([task(name=name)]) for name in ("", "Upper", "under_score", "a" * 41, "has space")]
        for data in invalid:
            with self.subTest(data=data[:32]), self.assertRaises(model.task_presets.TaskPresetError):
                model.task_presets.parse_task_presets(data)
        model.task_presets.parse_task_presets(encoded([task(name="a" * 40)]))

    def test_task_numbers_and_command_arguments(self):
        for timeout in (0.01, 3600):
            for limit in (128, 65536):
                model.task_presets.parse_task_presets(encoded([task(timeout=timeout, output_limit=limit)]))
        invalid = [task(timeout=x) for x in (0, 0.009, 3601, True, float("nan"), float("inf"))]
        invalid += [task(output_limit=x) for x in (127, 65537, True, 128.0)]
        invalid += [task(argv="text"), task(argv=[1]), task(argv=["x"] * 129),
                    task(executable="C:/BuilderExample/test.cmd", argv=["& echo"]),
                    task(cwd="relative"), task(description="bad\ntext")]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(model.task_presets.TaskPresetError):
                model.task_presets.parse_task_presets(encoded([value]))

    def test_read_only_cwd_is_warning_and_serializers_call_core(self):
        contents = {"allowed-roots.txt": b"read: C:/BuilderExample", "language.txt": b"ja",
                    "tasks.json": encoded([task()])}
        with patch.object(model.allowed_roots, "_plain_path", side_effect=lambda p, **kw: p), \
                patch.object(model.task_presets, "parse_task_presets", wraps=model.task_presets.parse_task_presets) as validator:
            result = model.validate_candidate(contents, ROOT, ENV)
        validator.assert_called_once_with(contents["tasks.json"])
        self.assertFalse(any(row[0] == "HOLD" for row in result))
        self.assertTrue(any(row[0] == "WARN" and "cwd" in row[2] for row in result))

    def test_templates_only_construct_commands(self):
        from config_builder.gui import task_template
        for kind in ("npm", "pytest"):
            value = task_template(kind, EXAMPLE, ENV)
            self.assertEqual(value["argv"][:3], ["/d", "/s", "/c"])
            self.assertEqual(Path(value["executable"]).name, "cmd.exe")
            model.task_presets.parse_task_presets(encoded([value]))


class InMemoryContractTests(unittest.TestCase):
    """UI controller/CLI contracts; no Tk window, configuration or scratch IO."""

    def editor(self):
        from config_builder.gui import Builder
        editor = Builder.__new__(Builder)
        editor.window = Mock()
        editor.t = lambda key: key
        return editor

    def test_check_reports_rows_and_exit_codes_without_saving(self):
        for level, expected in (("PASS", 0), ("WARN", 0), ("HOLD", 1)):
            rows = [(level, name, "synthetic") for name in model.NAMES]
            with self.subTest(level=level), \
                    patch.object(entry, "check_existing", return_value=rows) as check, \
                    patch.object(model, "save") as save, \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(entry.main(["--check"]), expected)
                self.assertEqual(output.getvalue().splitlines(),
                                 [f"[{level}] {name}: synthetic" for name in model.NAMES])
                check.assert_called_once_with(entry.ROOT)
                save.assert_not_called()

    def test_new_folder_defaults_to_read_only(self):
        editor = self.editor()
        editor.root_lines = []
        editor.roots_enabled = Mock()
        editor.update_roots = Mock()
        with patch("config_builder.gui.filedialog.askdirectory", return_value="C:/SyntheticProject"):
            editor.add_root()
        self.assertEqual(editor.root_lines, ["read: C:/SyntheticProject"])
        editor.roots_enabled.set.assert_called_once_with(True)
        editor.update_roots.assert_called_once_with()

    def test_skipped_root_is_a_localized_warning_and_does_not_block_save(self):
        contents = dict.fromkeys(model.NAMES)
        contents[model.NAMES[0]] = f"{EXAMPLE}\nread: {EXAMPLE.with_name('private-missing')}".encode()
        editor = self.editor()
        editor.contents, editor.root, editor.environ = contents, ROOT, ENV
        editor.form_dirty, editor.locale, editor.status = False, "ja", Mock()
        editor.baseline = {name: model.FileRecord(None, None) for name in model.NAMES}
        editor.ack, editor.save_button, editor.preview = Mock(), Mock(), Mock()
        editor.show = Mock()
        editor.capture_tasks = Mock()

        def inspect(path, **_):
            if path.name == "private-missing":
                raise model.allowed_roots._MissingFolder(path)
            return path

        with patch.object(model.allowed_roots, "_plain_path", side_effect=inspect), \
                patch.object(model, "validate_candidate", wraps=model.validate_candidate) as validate:
            editor.review()
        validate.assert_called_once()
        shown = editor.show.call_args_list[0].args[1]
        self.assertIn("[WARN] allowed-roots.txt: 2行目（0はファイル全体）: フォルダがありません。", shown)
        self.assertNotIn("private-missing", shown)
        self.assertEqual(editor.reviewed, contents)
        editor.ack.get.return_value = True
        editor.enable_save()
        editor.save_button.configure.assert_called_with(state="normal")

        contents[model.NAMES[0]] = f"{EXAMPLE}\nread: {EXAMPLE / 'private-missing'}".encode()
        editor.show.reset_mock()
        with patch.object(model.allowed_roots, "_plain_path", side_effect=inspect):
            editor.review()
        shown = editor.show.call_args_list[0].args[1]
        self.assertIn("[HOLD] allowed-roots.txt: 2行目（0はファイル全体）: "
                      "フルと読むだけのフォルダが同じ、または親子になっています。", shown)
        self.assertNotIn("private-missing", shown)
        self.assertIsNone(editor.reviewed)
        editor.enable_save()
        editor.save_button.configure.assert_called_with(state="disabled")

    def test_full_access_requires_explicit_confirmation(self):
        for approved in (False, True):
            with self.subTest(approved=approved):
                editor = self.editor()
                editor.roots_list = Mock()
                editor.roots_list.curselection.return_value = (0,)
                editor.root_indexes = [0]
                editor.root_lines = ["read: C:/SyntheticProject"]
                editor.update_roots = Mock()
                with patch("config_builder.gui.messagebox.askyesno", return_value=approved) as confirm:
                    editor.set_root_mode(False)
                confirm.assert_called_once()
                self.assertEqual(editor.root_lines, ["C:/SyntheticProject" if approved
                                                    else "read: C:/SyntheticProject"])
                self.assertEqual(editor.update_roots.call_count, int(approved))

    def test_draft_change_invalidates_review_and_acknowledgement(self):
        editor = self.editor()
        editor.reviewed = {"language.txt": b"ja"}
        editor.ack, editor.save_button, editor.preview, editor.show = Mock(), Mock(), Mock(), Mock()
        editor.pending_check = "previous-check"
        editor.changed()
        self.assertIsNone(editor.reviewed)
        editor.ack.set.assert_called_once_with(False)
        editor.save_button.configure.assert_called_once_with(state="disabled")
        editor.window.after_cancel.assert_called_once_with("previous-check")
        editor.window.after.assert_called_once_with(250, editor.validate)

    def test_review_contains_all_files_and_cannot_approve_invalid_draft(self):
        for valid in (False, True):
            with self.subTest(valid=valid):
                editor = self.editor()
                editor.contents = dict(zip(model.NAMES, (b"read: C:/SyntheticProject\n", None, b'{"tasks": []}\n')))
                editor.baseline = {name: model.FileRecord(None, None) for name in model.NAMES}
                editor.ack, editor.save_button, editor.preview, editor.show = Mock(), Mock(), Mock(), Mock()
                editor.validate = Mock(return_value=valid)
                editor.review()
                editor.validate.assert_called_once_with()
                self.assertEqual(editor.reviewed, editor.contents if valid else None)
                editor.ack.set.assert_called_once_with(False)
                preview = editor.show.call_args.args[1]
                for name in model.NAMES:
                    self.assertIn(name, preview)
                self.assertIn("read: C:/SyntheticProject", preview)
                self.assertIn('{"tasks": []}', preview)
                self.assertIn("absent", preview)

    def test_save_refuses_missing_acknowledgement_or_stale_review(self):
        for acknowledged, reviewed in ((False, {"language.txt": b"ja"}),
                                       (True, None), (True, {"language.txt": b"en"})):
            with self.subTest(acknowledged=acknowledged, reviewed=reviewed):
                editor = self.editor()
                editor.capture_tasks = Mock()
                editor.contents = {"language.txt": b"ja"}
                editor.reviewed = reviewed
                editor.ack = Mock()
                editor.ack.get.return_value = acknowledged
                with patch.object(model, "save") as save, \
                        patch("config_builder.gui.messagebox.askyesno") as confirm:
                    editor.save()
                save.assert_not_called()
                confirm.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows inherited synthetic files")
class StorageTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(self.enterContext(inherited_test_temp()))
        self.home = self.scratch / "profile"
        self.home.mkdir()
        self.env = dict(ENV, USERPROFILE=str(self.home), LOCALAPPDATA=str(self.home / "AppData/Local"))
        self.folder = self.home / ".config/sora-commander"
        real_plain = model.allowed_roots._plain_path
        real_resolve = Path.resolve

        def resolve(path, strict=False):
            if path == EXAMPLE or EXAMPLE in path.parents:
                return path
            return real_resolve(path, strict=strict)

        def metadata(path, *, directory):
            if directory and (path == EXAMPLE or EXAMPLE in path.parents):
                return path
            return real_plain(path, directory=directory)

        self.enterContext(patch.object(model.allowed_roots, "_plain_path", side_effect=metadata))
        self.enterContext(patch.object(Path, "resolve", resolve))
        self.enterContext(patch.object(entry, "ROOT", ROOT))
        self.contents = {"allowed-roots.txt": b"read: C:/BuilderExample\n", "language.txt": b"ja\n",
                         "tasks.json": encoded([task()])}

    def seed(self):
        self.folder.mkdir(parents=True)
        for name, data in self.contents.items():
            (self.folder / name).write_bytes(data)

    def test_new_files_and_startup_parity(self):
        baseline = model.snapshot(self.env)
        backups, result = model.save(self.contents, baseline, ROOT, self.env, acknowledged=True)
        self.assertEqual(backups, {})
        self.assertFalse(any(row[0] == "HOLD" for row in result))
        self.assertEqual({n: r.data for n, r in model.snapshot(self.env).items()}, self.contents)
        self.assertEqual(model.allowed_roots.startup_allowed_roots(ROOT, "development", self.env),
                         model.allowed_roots.parse_allowed_roots(self.contents["allowed-roots.txt"], self.env))
        self.assertEqual(model.task_presets.load_task_presets(self.env),
                         model.task_presets.parse_task_presets(self.contents["tasks.json"]))
        self.assertFalse((self.folder / ".config-builder.lock").exists())
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_backup_atomic_replace_and_confirmed_language_delete(self):
        self.seed()
        baseline = model.snapshot(self.env)
        changed = dict(self.contents, **{"language.txt": None, "tasks.json": encoded([task(name="changed")])})
        with patch.object(model.os, "replace", wraps=model.os.replace) as replace:
            backups, result = model.save(changed, baseline, ROOT, self.env, acknowledged=True)
        self.assertEqual(replace.call_count, 1)
        for name, path in backups.items():
            self.assertRegex(Path(path).name, r"\.bak-\d{8}-\d{6}$")
            self.assertEqual(Path(path).read_bytes(), self.contents[name])
        self.assertFalse((self.folder / "language.txt").exists())
        self.assertFalse(any(row[0] == "HOLD" for row in result))

    def test_without_acknowledgement_writes_nothing(self):
        with self.assertRaises(model.ConfigError):
            model.save(self.contents, model.snapshot(self.env), ROOT, self.env)
        self.assertFalse(self.folder.exists())

    def test_invalid_candidate_writes_nothing(self):
        bad = dict(self.contents, **{"language.txt": b"JA"})
        with self.assertRaises(model.ConfigError):
            model.save(bad, model.snapshot(self.env), ROOT, self.env, acknowledged=True)
        self.assertFalse(self.folder.exists())

    def test_external_change_is_preserved(self):
        self.seed()
        baseline = model.snapshot(self.env)
        (self.folder / "language.txt").write_bytes(b"en\n")
        with self.assertRaises(model.ConfigError):
            model.save(self.contents, baseline, ROOT, self.env, acknowledged=True)
        self.assertEqual((self.folder / "language.txt").read_bytes(), b"en\n")
        self.assertEqual(list(self.folder.glob("*.bak-*")), [])

    def test_read_accepts_path_handle_ctime_difference(self):
        self.seed()
        path = self.folder / "language.txt"
        real_fstat = model.os.fstat

        def different_ctime(fd):
            info = real_fstat(fd)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode",
                      "st_nlink", "st_file_attributes")
            return SimpleNamespace(**{name: getattr(info, name, 0) for name in fields},
                                   st_ctime_ns=path.lstat().st_ctime_ns + 269_000_000_000)

        with patch.object(model.os, "fstat", side_effect=different_ctime):
            record = model.read_record(path, model.LIMITS["language.txt"])
        self.assertEqual(record.data, b"ja\n")
        self.assertEqual(record.identity, model._identity(path.lstat()))

    def test_changes_during_read_are_refused(self):
        self.seed()
        path = self.folder / "language.txt"
        real_fstat = model.os.fstat
        for change in ("size", "mtime"):
            with self.subTest(change=change):
                path.write_bytes(b"ja\n")
                before = path.stat()
                calls = []

                def change_after_open(fd):
                    info = real_fstat(fd)
                    if not calls:
                        if change == "size":
                            path.write_bytes(b"english\n")
                        else:
                            os.utime(path, ns=(before.st_atime_ns,
                                               before.st_mtime_ns + 2_000_000_000))
                            self.assertEqual(path.stat().st_size, before.st_size)
                            self.assertNotEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
                    calls.append(info)
                    return info

                with patch.object(model.os, "fstat", side_effect=change_after_open), \
                        self.assertRaisesRegex(model.ConfigError, "Configuration changed while reading"):
                    model.read_record(path, model.LIMITS["language.txt"])
                self.assertEqual(len(calls), 2)

    def test_size_and_mtime_changes_since_snapshot_prevent_save(self):
        self.seed()
        path = self.folder / "language.txt"
        for change in ("size", "mtime"):
            with self.subTest(change=change):
                path.write_bytes(b"ja\n")
                baseline = model.snapshot(self.env)
                before = path.stat()
                if change == "size":
                    path.write_bytes(b"english\n")
                else:
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
                    self.assertEqual(path.stat().st_size, before.st_size)
                    self.assertNotEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
                preserved = path.read_bytes()
                with self.assertRaisesRegex(model.ConfigError, "Configuration changed since it was loaded"):
                    model.save(dict(self.contents, **{"language.txt": b"en\n"}), baseline,
                               ROOT, self.env, acknowledged=True)
                self.assertEqual(path.read_bytes(), preserved)
                self.assertEqual(list(self.folder.glob("*.bak-*")), [])
                self.assertFalse((self.folder / ".config-builder.lock").exists())

    def test_backup_failure_never_replaces_original(self):
        self.seed()
        baseline = model.snapshot(self.env)
        write = model._write_new

        def fail_backup(path, data):
            if ".bak-" in path.name:
                raise PermissionError("synthetic denial")
            return write(path, data)

        with patch.object(model, "_write_new", side_effect=fail_backup), \
                patch.object(model.os, "replace") as replace, \
                self.assertRaises(model.SaveError) as caught:
            model.save(dict(self.contents, **{"language.txt": b"en\n"}), baseline, ROOT, self.env, acknowledged=True)
        replace.assert_not_called()
        self.assertEqual(caught.exception.completed, ())
        self.assertEqual(model.snapshot(self.env), baseline)
        self.assertTrue((self.folder / ".config-builder.lock").exists())

    def test_partial_replace_reports_completed_and_preserves_backups(self):
        self.seed()
        changed = dict(self.contents, **{"language.txt": b"en\n", "tasks.json": encoded([task(name="changed")])})
        replace = model.os.replace

        def fail_second(source, target):
            if target.name == "tasks.json":
                raise OSError("synthetic replacement failure")
            return replace(source, target)

        with patch.object(model.os, "replace", side_effect=fail_second), self.assertRaises(model.SaveError) as caught:
            model.save(changed, model.snapshot(self.env), ROOT, self.env, acknowledged=True)
        self.assertEqual(caught.exception.completed, ("language.txt",))
        self.assertEqual((self.folder / "language.txt").read_bytes(), b"en\n")
        self.assertEqual((self.folder / "tasks.json").read_bytes(), self.contents["tasks.json"])
        self.assertEqual(set(caught.exception.backups), {"language.txt", "tasks.json"})

    def test_existing_lock_is_not_overwritten(self):
        self.seed()
        baseline = model.snapshot(self.env)
        lock = self.folder / ".config-builder.lock"
        lock.write_bytes(b"another builder")
        with self.assertRaises(FileExistsError):
            model.save(dict(self.contents, **{"language.txt": b"en\n"}), baseline, ROOT, self.env, acknowledged=True)
        self.assertEqual(lock.read_bytes(), b"another builder")
        self.assertEqual(model.snapshot(self.env), baseline)

    def test_existing_backup_collision_is_not_overwritten(self):
        self.seed()
        baseline = model.snapshot(self.env)
        backup = self.folder / "language.txt.bak-20260101-010203"
        backup.write_bytes(b"keep this earlier backup")
        with patch.object(model, "datetime") as clock:
            clock.now.return_value.strftime.return_value = "20260101-010203"
            with self.assertRaises(model.SaveError) as caught:
                model.save(dict(self.contents, **{"language.txt": b"en\n"}), baseline,
                           ROOT, self.env, acknowledged=True)
        self.assertEqual(caught.exception.completed, ())
        self.assertEqual(backup.read_bytes(), b"keep this earlier backup")
        self.assertEqual(model.snapshot(self.env), baseline)

    def test_postwrite_hold_is_reported_as_partial_save(self):
        self.seed()
        with patch.object(model, "check_existing", return_value=[("HOLD", "language.txt", "synthetic")]), \
                self.assertRaises(model.SaveError) as caught:
            model.save(dict(self.contents, **{"language.txt": b"en\n"}), model.snapshot(self.env),
                       ROOT, self.env, acknowledged=True)
        self.assertEqual(caught.exception.completed, ("language.txt",))
        self.assertEqual(Path(caught.exception.backups["language.txt"]).read_bytes(), b"ja\n")

    def test_changed_lock_reports_completed_save_without_deleting_foreign_lock(self):
        self.seed()
        baseline = model.snapshot(self.env)
        lock = self.folder / ".config-builder.lock"

        def change_lock(*args):
            lock.write_bytes(b"x" * 32)
            return [("PASS", name, "synthetic") for name in model.NAMES]

        with patch.object(model, "check_existing", side_effect=change_lock), \
                self.assertRaises(model.SaveError) as caught:
            model.save(dict(self.contents, **{"language.txt": b"en\n"}), baseline,
                       ROOT, self.env, acknowledged=True)
        self.assertEqual(caught.exception.completed, ("language.txt",))
        self.assertEqual(caught.exception.failure, "Save lock changed before completion.")
        self.assertEqual(lock.read_bytes(), b"x" * 32)
        self.assertEqual((self.folder / "language.txt").read_bytes(), b"en\n")
        self.assertEqual(Path(caught.exception.backups["language.txt"]).read_bytes(), b"ja\n")

    def test_lock_cleanup_error_reports_completed_save_without_retry(self):
        self.seed()
        baseline = model.snapshot(self.env)
        with patch.object(Path, "unlink", side_effect=OSError("synthetic cleanup failure")) as unlink, \
                self.assertRaises(model.SaveError) as caught:
            model.save(dict(self.contents, **{"language.txt": b"en\n"}), baseline,
                       ROOT, self.env, acknowledged=True)
        unlink.assert_called_once_with()
        self.assertEqual(caught.exception.completed, ("language.txt",))
        self.assertEqual((self.folder / "language.txt").read_bytes(), b"en\n")
        self.assertEqual(Path(caught.exception.backups["language.txt"]).read_bytes(), b"ja\n")
        self.assertTrue((self.folder / ".config-builder.lock").exists())

    def test_backup_tamper_prevents_replacement(self):
        self.seed()
        baseline = model.snapshot(self.env)
        write = model._write_new

        def tamper(path, data):
            write(path, b"tampered" if ".bak-" in path.name else data)

        with patch.object(model, "_write_new", side_effect=tamper), self.assertRaises(model.SaveError):
            model.save(dict(self.contents, **{"language.txt": b"en\n"}), baseline,
                       ROOT, self.env, acknowledged=True)
        self.assertEqual(model.snapshot(self.env), baseline)

    def test_staged_tamper_after_initial_verification_prevents_replacement(self):
        self.seed()
        baseline = model.snapshot(self.env)
        read = model.read_record
        tampered = []

        def tamper_after_staging(path, limit):
            record = read(path, limit)
            if path == self.folder / "language.txt" and not tampered:
                stages = list(self.folder.glob(".language.txt.*.tmp"))
                if stages:
                    self.assertEqual(len(stages), 1)
                    self.assertEqual(stages[0].read_bytes(), b"en\n")
                    stages[0].write_bytes(b"ja\n")
                    tampered.append(stages[0])
            return record

        with patch.object(model, "read_record", side_effect=tamper_after_staging), \
                patch.object(model.os, "replace") as replace, \
                self.assertRaises(model.SaveError) as caught:
            model.save(dict(self.contents, **{"language.txt": b"en\n"}), baseline,
                       ROOT, self.env, acknowledged=True)
        self.assertEqual(len(tampered), 1)
        replace.assert_not_called()
        self.assertEqual(caught.exception.completed, ())
        self.assertEqual(caught.exception.failure, "Staged content changed before replacement.")
        self.assertEqual(model.snapshot(self.env), baseline)
        self.assertEqual(Path(caught.exception.backups["language.txt"]).read_bytes(), b"ja\n")
        self.assertTrue((self.folder / ".config-builder.lock").exists())
        self.assertTrue(tampered[0].exists())

    def test_link_attributes_are_refused_without_following(self):
        for info in (SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400, st_nlink=1),
                     SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0, st_nlink=2)):
            with self.assertRaises(model.ConfigError):
                model._ordinary(info, False)

    def test_check_exit_codes_and_no_writes(self):
        self.seed()
        before = model.snapshot(self.env)
        with patch.dict(os.environ, self.env), redirect_stdout(io.StringIO()) as output, \
                patch.object(model, "_write_new", side_effect=AssertionError("check wrote a file")):
            self.assertEqual(entry.main(["--check"]), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 3)
        self.assertIn("[PASS] tasks.json", output.getvalue())
        self.assertEqual(model.snapshot(self.env), before)
        (self.folder / "language.txt").write_bytes(b"invalid")
        with patch.dict(os.environ, self.env), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(entry.main(["--check"]), 1)
        self.assertIn("[HOLD] language.txt", output.getvalue())

    def test_missing_check_does_not_create_settings(self):
        result = model.check_existing(ROOT, self.env)
        self.assertFalse(any(row[0] == "HOLD" for row in result))
        self.assertEqual(result[0][0], "WARN")
        self.assertFalse(self.folder.exists())


@unittest.skipUnless(os.name == "nt", "Windows inherited synthetic files")
class PackageTests(unittest.TestCase):
    def test_zip_contains_only_fixed_files_and_matching_hashes(self):
        from tools import build_config_builder as package
        with inherited_test_temp() as directory:
            root = Path(directory)
            for name in package.FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic package content\n")
            (root / "private-key.txt").write_bytes(b"synthetic excluded content")
            result = package.build(root)
            self.assertEqual(result, root / "artifacts/config-builder/config-builder.zip")
            with zipfile.ZipFile(result) as archive:
                self.assertEqual(set(archive.namelist()), set(package.FILES) | set(package.QUICKSTART) | {"MANIFEST.json"})
                for row in json.loads(archive.read("MANIFEST.json")):
                    data = archive.read(row["path"])
                    self.assertEqual(row["bytes"], len(data))
                    self.assertEqual(row["sha256"], hashlib.sha256(data).hexdigest())
            before = result.read_bytes()
            with self.assertRaises(FileExistsError):
                package.build(root)
            self.assertEqual(result.read_bytes(), before)

    # The public tree does not ship the development builder; same rule as
    # tests/test_scn_doctor_20261112.py. The checkout run still checks it.
    @unittest.skipUnless((Path(__file__).resolve().parents[1] / "tools/build_public_tree.py").is_file(),
                         "development checkout only")
    def test_normal_public_mapping_includes_companion(self):
        from tools import build_public_tree as public
        self.assertIn("tools/scn_config_builder.py", public.TOOLS)
        self.assertIn("tools/config_builder/model.py", public.TOOLS)
        self.assertEqual(public.MAPPED["docs/config-builder.ja.md"], "docs/config-builder.ja.md")


if __name__ == "__main__":
    unittest.main()
