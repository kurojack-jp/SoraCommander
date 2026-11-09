"""Isolated (-I -B) merged UI loader and transport-binding smoke.

Standalone:
  .venv\\Scripts\\python.exe -I -B tests\\test_owner_review_bound_isolated_20261101.py
Only fake Tk and synthetic JSON. Does not request or receive real Owner consent.
"""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "src" / "sora_commander_next"


def load_fixed_loader():
    path = SOURCE / "owner_review_isolated_candidate_loader.py"
    spec = importlib.util.spec_from_file_location("_scn_local_loader_smoke", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("missing fixed Owner loader")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def frame(*, v2=False):
    if not v2:
        data = dict(nonce="a" * 64, executable="powershell.exe",
                    argv=["-EncodedCommand", "QQ=="], cwd=r"D:\repo",
                    seconds=12, pending_id="isolated-smoke",
                    timeout=30, output_limit=1024)
    else:
        data = dict(nonce="a" * 64, review_id="v2-smoke",
                    spec_sha256="b" * 64, owner_label="Owner",
                    executable="git.exe", argv=["status"], cwd=r"D:\repo",
                    seconds=12, timeout_ms=20000, output_limit=1024,
                    allow_trusted=False, unattended=False)
    return (json.dumps(data) + "\n").encode("utf-8")


class IsolatedBoundReviewTests(unittest.TestCase):
    def test_isolated_mode_does_not_require_module_path_injection(self):
        # Standard unittest discovery imports other packages first;
        # isolation guarantees apply to the actual python -I smoke only.
        if sys.flags.isolated:
            self.assertNotIn(str(SOURCE.parent), sys.path)
            self.assertNotIn(str(SOURCE), sys.path)
        self.assertTrue(SOURCE.is_dir())
        self.assertFalse(SOURCE.is_symlink())

    def test_loader_uses_fixed_siblings_without_sys_path_changes(self):
        original_path = tuple(sys.path)
        loader = load_fixed_loader()
        candidate = loader.load_bound_review_candidate()
        self.assertIs(candidate, loader.load_bound_review_candidate())
        self.assertEqual(tuple(sys.path), original_path)
        self.assertTrue(Path(candidate.__file__).samefile(
            SOURCE / "owner_review_request_binding_candidate.py"))

    def test_both_command_types_bilingual_and_disabled_reusable_permission(self):
        loader = load_fixed_loader()
        candidate = loader.load_bound_review_candidate()
        for locale in ("en", "ja"):
            for kind in ("command", "v2_candidate"):
                with self.subTest(kind=kind, locale=locale):
                    v = (candidate.bind_command_frame(frame(), locale=locale)
                         if kind == "command"
                         else candidate.bind_v2_frame(frame(v2=True), locale=locale))
                    self.assertTrue(v.presentation.original_body)
                    self.assertTrue(v.presentation.screen.body.endswith(
                        v.presentation.original_body))
                    self.assertFalse(v.presentation.screen.trusted_offered)
                    tk, root, theme, deny, approve = (
                        Mock(), Mock(), Mock(), Mock(), Mock())
                    v.presentation.render(tk, root, theme,
                                          reject=deny, approve=approve)
                    theme.viewer.return_value.insert.assert_called_once_with(
                        "1.0", v.presentation.screen.body)
                    buttons = theme.neon_button.call_args_list
                    self.assertEqual(len(buttons), 3)
                    self.assertTrue(buttons[-1].kwargs["disabled"])
                    self.assertNotIn("command", buttons[-1].kwargs)
                    deny.assert_not_called()
                    approve.assert_not_called()

    def test_linked_dependency_is_rejected_even_after_loader_cache(self):
        loader = load_fixed_loader()
        loader.load_bound_review_candidate()
        original = Path.is_symlink

        def fake_link(path):
            return path.name == "owner_review_renderer.py" or original(path)

        with patch.object(Path, "is_symlink", autospec=True,
                          side_effect=fake_link):
            with self.assertRaisesRegex(ValueError, "linked"):
                loader.load_bound_review_candidate()

    def test_linked_ancestor_is_rejected_even_after_loader_cache(self):
        loader = load_fixed_loader()
        loader.load_bound_review_candidate()
        original = Path.is_junction

        def fake_junction(path):
            return path.name == "src" or original(path)

        with patch.object(Path, "is_junction", autospec=True,
                          side_effect=fake_junction):
            with self.assertRaisesRegex(ValueError, "linked ancestor"):
                loader.load_bound_review_candidate()

    def test_cache_path_poisoning_is_rejected(self):
        loader = load_fixed_loader()
        loader.load_bound_review_candidate()
        package = sys.modules[loader._PACKAGE_NAME]
        original = package.__path__
        try:
            package.__path__ = ["D:\\unexpected"]
            with self.assertRaisesRegex(ValueError, "untrusted cached"):
                loader.load_bound_review_candidate()
        finally:
            package.__path__ = original
        self.assertTrue(loader.load_bound_review_candidate())

    def test_invalid_locales_fail_without_gui(self):
        loader = load_fixed_loader()
        candidate = loader.load_bound_review_candidate()
        with self.assertRaises(ValueError):
            candidate.bind_command_frame(frame(), locale="fr")


if __name__ == "__main__":
    unittest.main(verbosity=1)
