"""Localized file review text: display data only. Fake Tk, no file, no write, no window."""
import ast
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next import approval_visual_theme as theme
from sora_commander_next import owner_file_review_localized_dev_20261104 as review
from sora_commander_next.file_edits import escape_review_display

OLD, NEW = "a" * 64, "b" * 64
PATH = r"D:\repo\日本語.txt"


def create_preview(**updates):
    return {"path": PATH, "bytes": 12, "new_sha256": NEW, "review": '"text"',
            "review_truncated": False, **updates}


def edit_preview(**updates):
    return {"path": PATH, "old_sha256": OLD, "new_sha256": NEW, "changed": True,
            "diff": "--- current\n+++ proposed\n-a\n+b\n", "diff_truncated": False, **updates}


def create(preview=None, **updates):
    options = dict(expected_path=PATH, expected_new_sha256=NEW, review_id="r-1", seconds=45.0,
                   locale="ja")
    return review.bound_create(preview or create_preview(), **{**options, **updates})


def edit(preview=None, **updates):
    options = dict(expected_path=PATH, expected_old_sha256=OLD, expected_new_sha256=NEW,
                   review_id="r-1", seconds=45.0, locale="ja")
    return review.bound_edit(preview or edit_preview(), **{**options, **updates})


def move(proposal=None, **updates):
    options = dict(expected_source=r"D:\a.txt", expected_destination=r"D:\b.txt",
                   review_id="r-1", seconds=45.0, locale="ja")
    data = proposal or {"operation": "move_file", "source": r"D:\a.txt", "destination": r"D:\b.txt"}
    return review.bound_move(data, **{**options, **updates})


class DisplayTests(unittest.TestCase):
    def test_edit_shows_path_both_sha_id_expiry_and_complete_diff(self):
        body = edit().presentation.screen.body
        for value in (escape_review_display(PATH), OLD, NEW, "'r-1'", "45",
                      "--- current\n+++ proposed\n-a\n+b\n"):
            self.assertIn(value, body)
        self.assertIn("現在のSHA-256", body)
        self.assertIn("変更後のSHA-256", body)

    def test_create_shows_path_size_sha_id_expiry_and_complete_content(self):
        body = create().presentation.screen.body
        for value in (escape_review_display(PATH), "12", NEW, "'r-1'", "45", '"text"'):
            self.assertIn(value, body)

    def test_move_shows_both_paths_id_and_expiry(self):
        body = move().presentation.screen.body
        for value in ("移動元", "移動先", escape_review_display(r"D:\a.txt"),
                      escape_review_display(r"D:\b.txt"), "'r-1'", "45"):
            self.assertIn(value, body)

    def test_english_uses_the_same_values(self):
        japanese, english = edit().presentation.screen.body, edit(locale="en").presentation.screen.body
        for value in (OLD, NEW, "--- current\n+++ proposed\n-a\n+b\n"):
            self.assertIn(value, japanese)
            self.assertIn(value, english)
        self.assertIn("Current SHA-256", english)
        self.assertNotIn("現在のSHA-256", english)

    def test_controls_in_a_path_or_diff_are_escaped_not_shown(self):
        body = edit(edit_preview(path="D:\\x\u202ey", diff="+a\u202eb\x07\n"),
                    expected_path="D:\\x\u202ey").presentation.screen.body
        self.assertNotIn("\u202e", body)
        self.assertNotIn("\x07", body)
        self.assertIn("\\u202E", body)

    def test_controls_in_every_operation_path_are_escaped_not_shown(self):
        odd = "D:\\x\u202ey"
        bodies = (
            create(create_preview(path=odd), expected_path=odd).presentation.screen.body,
            move({"operation": "move_file", "source": odd, "destination": r"D:\b.txt"},
                 expected_source=odd).presentation.screen.body,
            move({"operation": "move_file", "source": r"D:\a.txt", "destination": odd},
                 expected_destination=odd).presentation.screen.body,
        )
        for body in bodies:
            self.assertNotIn("\u202e", body)
            self.assertIn("\\u202E", body)

    def test_only_reject_and_approve_once_can_be_active(self):
        for bound in (create(), edit(), move()):
            screen = bound.presentation.screen
            self.assertFalse(screen.trusted_offered)
            self.assertIn(screen.kind, ("file_create", "file_edit"))
            self.assertEqual(bound.presentation.labels[:2], ("却下", "今回だけ承認"))
            self.assertIsNone(bound.presentation.caution_level)

    def test_rendering_keeps_each_callback_and_disabled_third_button(self):
        tk = Mock()
        root = tk.Tk.return_value
        root.winfo_screenwidth.return_value = 1366
        root.winfo_screenheight.return_value = 768
        reject, approve = Mock(), Mock()
        edit().presentation.render(tk, root, theme, reject=reject, approve=approve)
        calls = tk.Button.call_args_list
        self.assertEqual([c.kwargs["text"] for c in calls[:2]], ["却下", "今回だけ承認"])
        self.assertIs(calls[0].kwargs["command"], reject)
        self.assertIs(calls[1].kwargs["command"], approve)
        self.assertEqual(calls[2].kwargs["state"], "disabled")
        self.assertNotIn("command", calls[2].kwargs)
        tk.Button.return_value.focus_set.assert_called_once_with()


class BindingTests(unittest.TestCase):
    def test_a_mismatched_target_is_refused(self):
        cases = [
            lambda: edit(expected_path=r"D:\other.txt"),
            lambda: edit(expected_old_sha256="c" * 64),
            lambda: edit(expected_new_sha256="c" * 64),
            lambda: edit(edit_preview(new_sha256="c" * 64)),
            lambda: edit(edit_preview(old_sha256="c" * 64)),
            lambda: edit(edit_preview(path=r"D:\other.txt")),
            lambda: create(expected_path=r"D:\other.txt"),
            lambda: create(expected_new_sha256="c" * 64),
            lambda: create(create_preview(new_sha256="c" * 64)),
            lambda: move(expected_source=r"D:\other.txt"),
            lambda: move(expected_destination=r"D:\other.txt"),
            lambda: move({"operation": "move_file", "source": r"D:\a.txt", "destination": r"D:\a.txt"},
                         expected_destination=r"D:\a.txt"),
        ]
        for index, case in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                case()

    def test_incomplete_or_malformed_proposals_are_refused(self):
        cases = [
            lambda: edit(edit_preview(diff_truncated=True)),
            lambda: edit(edit_preview(diff_truncated="false")),
            lambda: edit(edit_preview(changed=False)),
            lambda: edit(edit_preview(diff="")),
            lambda: edit(edit_preview(old_sha256="A" * 64), expected_old_sha256="A" * 64),
            lambda: create(create_preview(review_truncated=True)),
            lambda: create(create_preview(bytes=-1)),
            lambda: create(create_preview(bytes=True)),
            lambda: create(create_preview(review="")),
            lambda: move({"operation": "move_file", "source": r"D:\a.txt"}),
            lambda: move({"operation": "create_file", "source": r"D:\a.txt", "destination": r"D:\b.txt"}),
            lambda: move({"operation": "move_file", "source": r"D:\a.txt",
                          "destination": r"D:\b.txt", "overwrite": True}),
            lambda: edit(review_id=""),
            lambda: edit(seconds=0),
            lambda: edit(seconds=61),
            lambda: edit(seconds=True),
            lambda: edit(locale="fr"),
        ]
        for index, case in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                case()

    def test_digest_changes_with_any_bound_field_but_never_with_language(self):
        base = edit().binding_sha256
        self.assertEqual(base, edit(locale="en").binding_sha256)
        for changed in (edit(review_id="r-2"), edit(seconds=44.0),
                        edit(edit_preview(diff="+different\n")),
                        edit(edit_preview(path=r"D:\o.txt"), expected_path=r"D:\o.txt"),
                        edit(edit_preview(old_sha256="c" * 64), expected_old_sha256="c" * 64),
                        edit(edit_preview(new_sha256="c" * 64), expected_new_sha256="c" * 64)):
            self.assertNotEqual(base, changed.binding_sha256)

    def test_module_has_no_way_to_write_decide_or_open_a_window(self):
        tree = ast.parse(Path(review.__file__).read_text(encoding="utf-8"))
        modules = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                   for alias in node.names}
        self.assertEqual(modules, {"json", "re"})
        taken = {alias.name for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module == "file_edits"
                 for alias in node.names}
        self.assertEqual(taken, {"escape_review_display"})
        called = {getattr(node.func, "attr", getattr(node.func, "id", ""))
                  for node in ast.walk(tree) if isinstance(node, ast.Call)}
        self.assertFalse(called & {"open", "write", "write_text", "write_bytes", "replace", "Tk",
                                   "approve_local", "reject_local", "run"})


if __name__ == "__main__":
    unittest.main()
