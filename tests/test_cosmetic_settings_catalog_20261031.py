"""No-IO allowlist regression for proposed settings. No GUI, files, sound."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sora_commander_next import cosmetic_settings_catalog as settings


class SettingsAllowlistTests(unittest.TestCase):
    def test_catalog_is_explicit_readonly_and_unpublished(self):
        result = settings.evaluate_settings_request(action="list")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["schema_version"], 1)
        self.assertIs(result["registered_mcp_tool"], False)
        self.assertEqual(len(result["settings"]), 1)
        self.assertEqual(result["settings"][0], {
            "key": "ui.owner_dialog.portrait",
            "type": "image/png",
            "default": "bundled_sora_256",
            "effective": None,
            "effective_verified": False,
            "availability": "DESIGN_ONLY",
            "writable": False,
        })

    def test_reads_allowlisted_key_only(self):
        result = settings.evaluate_settings_request(
            action="get", key="ui.owner_dialog.portrait")
        self.assertIsNone(result["setting"]["effective"])
        self.assertFalse(result["setting"]["effective_verified"])
        self.assertEqual(result["setting"]["default"], "bundled_sora_256")
        self.assertFalse(result["setting"]["writable"])

    def test_get_invalid_key_types_and_nonexistent_names_fail(self):
        bad_keys = (None, False, 0, 1, b"ui.owner_dialog.portrait", [],
                    {}, "ui.owner_dialog", "ui.owner_dialog.portrait ",
                    "ui.owner_dialog.portrait\0", "auth.token",
                    "policy.roots", "permissions", "service.enabled",
                    "notifications.success_sound", "__class__")
        for key in bad_keys:
            with self.subTest(key=repr(key)):
                with self.assertRaises(settings.SettingsRequestError):
                    settings.evaluate_settings_request(action="get", key=key)

    def test_boolean_and_other_actions_never_coerce(self):
        for action in (None, True, False, 3, b"list", [], {},
                       "LIST", "list ", "", "enable", "delete", "set",
                       "run_command", "config", "set_config", "exec"):
            with self.subTest(action=repr(action)):
                with self.assertRaises(settings.SettingsRequestError):
                    settings.evaluate_settings_request(action=action)

    def test_list_with_extras_is_rejected_even_when_none(self):
        for kwargs in (
            {"key": None}, {"value": None}, {"preview_id": None},
            {"key": "ui.owner_dialog.portrait"},
            {"value": "anything"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(settings.SettingsRequestError):
                    settings.evaluate_settings_request(action="list", **kwargs)

    def test_get_with_mutation_fields_is_rejected(self):
        for kwargs in ({"value": ""}, {"value": None},
                       {"preview_id": None}, {"preview_id": "token"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(settings.SettingsRequestError):
                    settings.evaluate_settings_request(
                        action="get", key="ui.owner_dialog.portrait", **kwargs)

    def test_all_write_actions_are_disabled_with_explicit_result(self):
        for action, kwargs in (
            ("preview", {"value": r"C:\Images\image.png"}),
            ("apply", {"preview_id": "candidate-123"}),
            ("reset", {}),
        ):
            with self.subTest(action=action):
                response = settings.evaluate_settings_request(
                    action=action, key="ui.owner_dialog.portrait", **kwargs)
                self.assertEqual(response["status"], "NOT_ENABLED")
                self.assertFalse(response["changed"])
                self.assertFalse(response["approval_granted"])
                self.assertFalse(response["source_validated"])
                self.assertNotIn("write_authorized", response)
                self.assertNotIn("preview_token", response)

    def test_preview_requires_literal_nonempty_value_not_token(self):
        for kwargs in ({"value": None}, {"value": True}, {"value": 1},
                       {"value": ""}, {"value": b"fake"},
                       {"value": ["x"]},
                       {"value": "x", "preview_id": "not allowed"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(settings.SettingsRequestError):
                    settings.evaluate_settings_request(
                        action="preview", key="ui.owner_dialog.portrait", **kwargs)

    def test_preview_rejects_urls_device_unc_ads_and_traversal(self):
        unsafe = (
            r"\\server\share\portrait.png",
            r"\\?\C:\portrait.png",
            r"C:relative.png",
            r"C:/Images/portrait.png",
            r"C:\Images\..\portrait.png",
            r"C:\Images\.\portrait.png",
            r"C:\Images\portrait.png:stream",
            r"C:\Images\CON.png",
            r"C:\Images\COM1.png",
            r"C:\Images\portrait.jpg",
            r"C:\Images\bad?.png",
            r"C:\Images\portrait.png ",
            "https://example.com/portrait.png",
            "data:image/png;base64,AA",
            "portrait.png",
            "C:\\Images\\" + "x" * 1025 + ".png",
        )
        for candidate in unsafe:
            with self.subTest(candidate=candidate[:60]):
                with self.assertRaises(settings.SettingsRequestError):
                    settings.evaluate_settings_request(
                        action="preview", key="ui.owner_dialog.portrait",
                        value=candidate)

    def test_apply_requires_literal_token_without_value(self):
        for kwargs in ({}, {"preview_id": None}, {"preview_id": ""},
                       {"preview_id": 0}, {"preview_id": True},
                       {"preview_id": []},
                       {"preview_id": "a", "value": "anything"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(settings.SettingsRequestError):
                    settings.evaluate_settings_request(
                        action="apply", key="ui.owner_dialog.portrait", **kwargs)

    def test_reset_rejects_unneeded_fields(self):
        for kwargs in ({"value": None}, {"value": ""}, {"preview_id": ""},
                       {"preview_id": "candidate-123"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(settings.SettingsRequestError):
                    settings.evaluate_settings_request(
                        action="reset", key="ui.owner_dialog.portrait", **kwargs)

    def test_mutation_of_unknown_keys_never_reaches_design_disabled(self):
        for key in ("auth.token", "policy.roots", "ui.owner_dialog.timeout"):
            for action, kwargs in (
                ("preview", {"value": "example.png"}),
                ("apply", {"preview_id": "id"}),
                ("reset", {}),
            ):
                with self.subTest(key=key, action=action):
                    with self.assertRaises(settings.SettingsRequestError):
                        settings.evaluate_settings_request(
                            action=action, key=key, **kwargs)

    def test_catalog_does_not_leak_mutable_singletons(self):
        first = settings.list_settings()
        first["settings"][0]["writable"] = True
        first["settings"].append({"key": "auth.token", "writable": True})
        second = settings.list_settings()
        self.assertEqual(len(second["settings"]), 1)
        self.assertFalse(second["settings"][0]["writable"])

    def test_no_generic_extra_keywords_are_accepted(self):
        for kwargs in ({"path": "C:\\secrets"}, {"root": "C:\\"},
                       {"enabled": True}, {"timeout": 0},
                       {"owner": "Alice"}, {"authorization": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(TypeError):
                    settings.evaluate_settings_request(action="list", **kwargs)

    def test_whitelist_is_not_extendable_through_public_dispatch(self):
        with self.assertRaises(TypeError):
            settings.evaluate_settings_request(
                action="list", catalogue={"auth.token": "YES"})
        self.assertNotIn("auth.token", settings._ALLOWLIST)

    def test_imports_are_stdlib_only_and_no_runtime_files_referenced(self):
        source = Path(settings.__file__).read_text(encoding="utf-8")
        for forbidden in ("tkinter", "winsound", "subprocess", "os.system",
                          "open(", "unlink(", "os.replace(", "chmod(", "Set-Acl"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
