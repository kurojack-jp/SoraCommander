"""DEV-only same-preview file presentation tests, no Owner operation."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sora_commander_next.owner_review_file_binding_candidate import (
    bind_file_create_preview, bind_file_edit_preview,
)


class FileBindingCandidateTests(unittest.TestCase):
    def setUp(self):
        self.create = dict(path=r"D:\safe\new.txt", bytes=5,
                           new_sha256="a"*64, review='"hello"',
                           review_truncated=False)
        self.edit = dict(path=r"D:\safe\old.txt",
                         diff="@@ -1 +1 @@\n-old\n+new\n",
                         diff_truncated=False)

    def test_both_locales_preserve_one_original(self):
        for locale in ("ja", "en"):
            for bind, preview, kind, evidence in (
                (bind_file_create_preview, self.create,
                 "file_create", '"hello"'),
                (bind_file_edit_preview, self.edit,
                 "file_edit", "+new\n"),
            ):
                with self.subTest(locale=locale, kind=kind):
                    result = bind(preview, locale=locale)
                    self.assertEqual(result.kind, kind)
                    self.assertTrue(result.presentation.original_body.endswith(evidence))
                    self.assertTrue(result.presentation.screen.body.endswith(
                        result.presentation.original_body))
                    self.assertFalse(result.presentation.screen.trusted_offered)

    def test_changed_caller_dict_does_not_change_display(self):
        result = bind_file_create_preview(self.create, locale="ja")
        before = result.presentation.screen.body
        self.create["path"] = "CHANGED"
        self.create["review"] = "CHANGED"
        self.assertEqual(result.presentation.screen.body, before)

    def test_tampered_or_partial_create_is_rejected(self):
        for update in ({"review_truncated": True},
                       {"new_sha256": "INVALID"},
                       {"bytes": -1},
                       {"extra": "x"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                bind_file_create_preview(dict(self.create, **update), locale="en")

    def test_edit_truncation_requires_complete_review(self):
        truncated = dict(self.edit, diff_truncated=True)
        with self.assertRaises(ValueError):
            bind_file_edit_preview(truncated, locale="ja")
        result = bind_file_edit_preview(truncated, locale="ja",
                                        complete_review="FULL SOURCE")
        self.assertIn("FULL SOURCE", result.presentation.original_body)
        self.assertNotIn("+new", result.presentation.original_body)

    def test_mismatch_type_or_extra_fields_rejected(self):
        with self.assertRaises(ValueError):
            bind_file_edit_preview(dict(self.edit, diff_truncated=1), locale="en")
        with self.assertRaises(ValueError):
            bind_file_edit_preview(dict(self.edit, extra="x"), locale="en")
        with self.assertRaises(ValueError):
            bind_file_edit_preview(self.edit, locale="xx")
        with self.assertRaises(ValueError):
            bind_file_edit_preview(self.edit, locale="ja", complete_review=42)

    def test_distinct_previews_have_distinct_diagnostic_digest(self):
        first = bind_file_edit_preview(self.edit, locale="ja")
        second = bind_file_edit_preview(dict(self.edit, diff="another"),
                                        locale="ja")
        self.assertNotEqual(first.preview_sha256, second.preview_sha256)


if __name__ == "__main__":
    unittest.main()
