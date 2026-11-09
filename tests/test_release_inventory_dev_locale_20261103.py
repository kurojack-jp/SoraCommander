"""Non-secret synthetic worktree inventory; no GUI, auth or live deployment."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests._inherited_test_temp import inherited_test_temp
from sora_commander_next import release_inventory as inventory


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / inventory.PACKAGE
PY_NAMES = {
    "owner_locale_frame_display_candidate.py",
    "owner_locale_settings_gui_candidate.py",
    "owner_review_locale_settings_candidate.py",
}
PINS = {
    "settings_en.dev.json": "ecb852c881b893cdcec6c7821a0f7dcb374ed7f0ce3a841356d0855b6aab35f9",
    "settings_ja.dev.json": "f2116548507d196d141fb2bcc0e79ea8213fafdc34ae64050c3cbe252c145275",
}


class DevLocaleInventoryTests(unittest.TestCase):
    def setUp(self):
        # The pinned JSON is worktree-only, so a clean checkout has nothing to pin.
        if not all((SOURCE / name).is_file() for name in PINS):
            self.skipTest("worktree-only locale JSON absent")
        self.home = Path(self.enterContext(inherited_test_temp()))
        self.source = self.home / "source"
        self.source.mkdir()
        for name in set(inventory.MODULES) | inventory.DEV_ONLY_MODULES:
            (self.source / name).write_bytes(b"# synthetic module\n")
        for name, expected in PINS.items():
            data = (SOURCE / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
            (self.source / name).write_bytes(data)
        self.destination = self.home / "candidate"

    def reject(self):
        with self.assertRaises(inventory.CandidateIntegrityError):
            inventory.make_candidate(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_current_tree_and_exact_dev_classification(self):
        self.assertEqual(inventory.OPTIONAL_DEV_ONLY_MODULES, PY_NAMES)
        self.assertEqual(inventory.OPTIONAL_DEV_ONLY_ASSET_SHA256, PINS)
        self.assertTrue(PY_NAMES <= inventory.DEV_ONLY_MODULES)
        self.assertFalse(PY_NAMES & set(inventory.MODULES))
        self.assertEqual(set(inventory._package_sources(SOURCE)), set(inventory.MODULES))

    def test_clean_tree_without_all_five_is_valid(self):
        for name in PY_NAMES | PINS.keys():
            (self.source / name).unlink()
        self.assertEqual(set(inventory._package_sources(self.source)), set(inventory.MODULES))

    def test_python_candidates_are_individually_optional(self):
        for name in sorted(PY_NAMES):
            (self.source / name).unlink()
            self.assertEqual(set(inventory._package_sources(self.source)), set(inventory.MODULES))

    def test_either_single_json_is_rejected(self):
        for name in PINS:
            with self.subTest(name=name):
                path = self.source / name
                data = path.read_bytes()
                path.unlink()
                self.reject()
                path.write_bytes(data)

    def test_tampered_and_crlf_bytes_are_rejected(self):
        for name in PINS:
            path = self.source / name
            original = path.read_bytes()
            for data in (original + b" ", original.replace(b"\n", b"\r\n")):
                with self.subTest(name=name, digest=hashlib.sha256(data).hexdigest()):
                    self.assertNotEqual(data, original)
                    path.write_bytes(data)
                    self.reject()
            path.write_bytes(original)

    def test_oversized_json_is_rejected(self):
        (self.source / "settings_en.dev.json").write_bytes(b" " * 8193)
        self.reject()

    def test_unknown_python_and_json_still_fail_closed(self):
        for name in ("unknown.py", "unknown.json", "settings_fr.dev.json"):
            with self.subTest(name=name):
                path = self.source / name
                path.write_bytes(b"{}")
                self.reject()
                path.unlink()

    def test_missing_required_dev_module_still_fails(self):
        (self.source / "owner_review_i18n.py").unlink()
        self.reject()

    def test_json_directory_is_rejected(self):
        path = self.source / "settings_en.dev.json"
        path.unlink()
        path.mkdir()
        self.reject()

    def test_symlink_is_rejected_without_os_link_privilege(self):
        # Model link metadata only; no denied native symlink operation or retry.
        original = Path.is_symlink
        for name in PY_NAMES | PINS.keys():
            with self.subTest(name=name), patch.object(
                    Path, "is_symlink",
                    lambda path: path == self.source / name or original(path)):
                self.reject()

    def test_other_reparse_points_are_rejected(self):
        original = Path.lstat
        for name in PY_NAMES | PINS.keys():
            def metadata(path, *args, **kwargs):
                if path == self.source / name:
                    return SimpleNamespace(st_file_attributes=0x400)
                return original(path, *args, **kwargs)
            with self.subTest(name=name), patch.object(Path, "lstat", metadata):
                self.reject()

    def test_candidate_and_manifest_never_include_five(self):
        digest = inventory.make_candidate(self.source, self.destination)
        self.assertTrue(inventory.verify_candidate(
            self.destination, trusted_manifest_sha256=digest))
        package = self.destination / inventory.PACKAGE
        self.assertEqual({item.name for item in package.iterdir()}, set(inventory.MODULES))
        manifest = json.loads((self.destination / inventory.MANIFEST).read_bytes())
        self.assertEqual({record["path"] for record in manifest["files"]},
                         {f"{inventory.PACKAGE}/{name}" for name in inventory.MODULES})
        for name in PY_NAMES | PINS.keys():
            path = package / name
            path.write_bytes(b"# synthetic injected DEV content")
            with self.subTest(name=name), self.assertRaises(inventory.CandidateIntegrityError):
                inventory.verify_candidate(self.destination, trusted_manifest_sha256=digest)
            path.unlink()


if __name__ == "__main__":
    unittest.main()
