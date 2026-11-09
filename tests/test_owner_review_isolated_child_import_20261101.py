"""Windows python -I isolated child import smoke. No real UI or Owner action.

Run as:
    .venv\\Scripts\\python.exe -I -B tests\\test_owner_review_isolated_child_import_20261101.py
"""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1] / "src" / "sora_commander_next"


def load_fixed_sibling(name):
    path = SOURCE / name
    spec = importlib.util.spec_from_file_location("_scn_ui_isolation_" + name.replace(".", "_"), path)
    if spec is None or spec.loader is None:
        raise RuntimeError("missing local Owner module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IsolatedChildReadOnlyTests(unittest.TestCase):
    def test_no_package_path_injected(self):
        # Path injection would undermine the isolated child import assumption.
        source = str(SOURCE)
        self.assertNotIn(source, sys.path)
        self.assertTrue(SOURCE.is_dir())

    def test_both_children_load_without_gui_or_private_pipe_access(self):
        for name in ("owner_approval_dialog.py", "owner_approval_dialog_v2.py"):
            with self.subTest(name=name):
                mod = load_fixed_sibling(name)
                self.assertTrue(callable(mod.main))
                self.assertTrue(callable(mod.parse_request if name.startswith("owner_approval_dialog.py")
                                         else mod.parse_review))

    def test_pinned_japanese_english_resources_load_under_isolation(self):
        i18n = load_fixed_sibling("owner_review_i18n.py")
        self.assertEqual(i18n.available_locales(), ("en", "ja"))
        for locale in ("en", "ja"):
            with self.subTest(locale=locale):
                draft = i18n.prepare_localized_review(
                    locale=locale, kind="file_edit",
                    original_body="PATH: x\\nEXACT CONTENT",
                    facts={"path": r"D:\\repo\\x.txt"})
                self.assertTrue(draft.body.endswith("PATH: x\\nEXACT CONTENT"))
                self.assertIn("x.txt", draft.summary)
                self.assertEqual(draft.render_kwargs()["kind"], "file_edit")
        self.assertEqual(i18n.load_catalog("ja")["approve_once"], "今回だけ承認")

    def test_command_caution_indicator_loads_under_isolated_child(self):
        caution = load_fixed_sibling("owner_command_caution.py")
        for locale in ("en", "ja"):
            with self.subTest(locale=locale):
                marker = caution.command_caution(
                    executable=r"C:\Windows\System32\cmd.exe",
                    argv=["/c", "echo", "hello"], locale=locale)
                self.assertEqual(marker.level, "ATTENTION")
                self.assertTrue(marker.icon)
                self.assertTrue(marker.disclaimer)
                self.assertFalse(hasattr(marker, "approved"))

    def test_shared_renderer_loads_for_two_local_review_kinds(self):
        theme = load_fixed_sibling("approval_visual_theme.py")
        for kind, trusted in (("command", False), ("v2_candidate", False)):
            with self.subTest(kind=kind):
                tk, root = Mock(), Mock()
                root.winfo_screenwidth.return_value = 1366
                root.winfo_screenheight.return_value = 768
                reject, approve = Mock(), Mock()
                payload = ("\r\n完整な本文: " + kind + "\n") * 1000
                pane = theme.shared_review(
                    tk, root, kind=kind, title="Owner Review",
                    heading="Exact local command", caution="Read all fields",
                    information="No click never approves",
                    body=payload, reject=reject, approve=approve,
                    trusted_offered=trusted)
                self.assertIs(pane, tk.Text.return_value)
                pane.insert.assert_called_once_with("1.0", payload)
                pane.configure.assert_any_call(state="disabled")
                self.assertEqual(pane.configure.call_count, 2)
                reject.assert_not_called()
                approve.assert_not_called()
                self.assertEqual(len(tk.Button.call_args_list), 3)
                self.assertEqual(tk.Button.call_args_list[-1].kwargs["state"], "disabled")


if __name__ == "__main__":
    unittest.main(verbosity=1)
