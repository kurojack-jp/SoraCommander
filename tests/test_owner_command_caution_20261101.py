"""Command visual caution is advisory only: fake input, no command/GUI/Owner."""
import ast
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.owner_command_caution import (
    CommandCaution, load_caution_catalog, command_caution,
)


class CommandCautionTests(unittest.TestCase):
    def assess(self, path, argv=None, locale="ja"):
        return command_caution(executable=path, argv=[] if argv is None else argv,
                               locale=locale)

    def test_unknown_is_not_safe_or_low(self):
        for path, argv in ((r"C:\Tools\my-app.exe", []),
                           (r"C:\Program Files\Git\cmd\git.exe", ["status"]),
                           (r"C:\Windows\System32\whoami.exe", ["/all"])):
            with self.subTest(path=path):
                card = self.assess(path, argv)
                self.assertEqual(card.level, "UNKNOWN")
                self.assertEqual(card.icon, "❔")
                self.assertIn("判定不能", card.display_lines()[0])
                self.assertIn("必ず元のコマンドと引数", card.disclaimer)
                self.assertEqual(len(card.display_lines()), 3)
                self.assertNotIn("safe", card.level.lower())

    def test_interpreter_and_nested_commands_get_attention_not_permission(self):
        cases = (
            (r"C:\Python\python.exe", ["-B", "-m", "unittest"]),
            (r"C:\Windows\System32\cmd.exe", ["/c", "echo", "hello"]),
            (r"C:\Program Files\PowerShell\7\pwsh.exe", ["-Command", "Write-Output 'ok'"]),
            (r"C:\Windows\System32\wscript.exe", ["script.vbs"]),
        )
        for path, argv in cases:
            with self.subTest(path=path):
                card = self.assess(path, argv)
                self.assertEqual(card.level, "ATTENTION")
                self.assertEqual(card.icon, "⚠️")
                self.assertTrue(card.reasons)
                self.assertTrue(card.disclaimer)

    def test_encoded_and_destructive_shell_tokens_raise_strong_caution(self):
        for path, argv in (
            (r"C:\Program Files\PowerShell\7\pwsh.exe", ["-EncodedCommand", "QQBCAA=="]),
            ("powershell.exe", ["-enc", "QQBCAA=="]),
            ("powershell.exe", ["-Command", "Remove-Item -Force 'C:\\temp\\x'"]),
            ("cmd.exe", ["/c", "reg delete HKCU\\X"]),
            (r"C:\Windows\System32\diskpart.exe", ["/s", "my-script.txt"]),
            (r"C:\Windows\System32\reg.exe", ["delete", r"HKCU\A"]),
            ("git.exe", ["push", "origin", "some-branch"]),
            ("git.exe", ["reset", "--hard"]),
            ("git.exe", ["clean", "-fdx"]),
        ):
            with self.subTest(executable=path, argv=argv):
                card = self.assess(path, argv)
                self.assertEqual(card.level, "STRONG")
                self.assertEqual(card.icon, "🔶")
                self.assertTrue(card.reasons)

    def test_only_visible_launch_clues_are_considered(self):
        # Do NOT claim to inspect a script: it could delete files after launch.
        card = self.assess("python.exe", ["arbitrary.py"])
        self.assertEqual(card.level, "ATTENTION")
        self.assertNotIn("安全です", "\n".join(card.display_lines()))
        self.assertIn("安全判定", card.disclaimer)
        # A string inside Python's arguments does not make it a PowerShell command.
        card = self.assess("python.exe", ["-c", "print('Remove-Item')"])
        self.assertEqual(card.level, "ATTENTION")

    def test_bilingual_fixed_json_and_no_hidden_command_data_in_indicator(self):
        en = load_caution_catalog("en")
        ja = load_caution_catalog("ja")
        self.assertEqual(set(en), set(ja))
        self.assertEqual(len(en), 12)
        self.assertEqual(ja["unknown"], "判定不能")
        self.assertEqual(en["unknown"], "Cannot determine")
        en["attention"] = "tampered"
        self.assertNotEqual(load_caution_catalog("en")["attention"], "tampered")
        exe = "python.exe"
        sensitive = "example-secret-token"
        for locale in ("en", "ja"):
            card = self.assess(exe, ["-c", sensitive], locale)
            self.assertIsInstance(card, CommandCaution)
            self.assertNotIn(sensitive, "\n".join(card.display_lines()))
            self.assertEqual(card.level, "ATTENTION")

    def test_invalid_or_untrusted_inputs_are_not_silently_reclassified(self):
        for locale in ("fr", "EN", "", "../en", None, True):
            with self.subTest(locale=locale), self.assertRaises(ValueError):
                self.assess("python.exe", [], locale)
        for exe in ("", "a\x00b", None, 1, "a" * 1025):
            with self.subTest(exe=exe), self.assertRaises(ValueError):
                command_caution(executable=exe, argv=[], locale="en")
        for argv in ("-Command", [None], [1], [True], ["" * 8193 + "X" * 8194],
                     ["" for _ in range(129)], ["bad\x00arg"]):
            with self.subTest(argv=repr(argv)[:120]), self.assertRaises(ValueError):
                command_caution(executable="cmd.exe", argv=argv, locale="en")

    def test_dictionary_integrity_or_link_failure_is_not_ignored(self):
        for locale in ("en", "ja"):
            with self.subTest(locale=locale):
                with patch.object(Path, "read_bytes", return_value=b"{}"):
                    with self.assertRaisesRegex(ValueError, "integrity"):
                        load_caution_catalog(locale)
                with patch.object(Path, "is_symlink", return_value=True):
                    with self.assertRaisesRegex(ValueError, "linked"):
                        load_caution_catalog(locale)

    def test_presentation_module_has_no_execution_or_owner_api(self):
        source = ROOT / "src" / "sora_commander_next" / "owner_command_caution.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        attrs = [node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        for forbidden in ("Tk", "Popen", "run", "system", "write_text",
                          "write_bytes", "approve", "read_text", "unlink"):
            self.assertNotIn(forbidden, attrs)
        self.assertNotIn("owner_review_dispatch", source.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
