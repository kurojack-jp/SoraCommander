"""Window language: language.txt first, else the Windows display language."""
import ast
from pathlib import Path
import sys
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from _inherited_test_temp import inherited_test_temp  # noqa: E402
from sora_commander_next import owner_ui_language as lang  # noqa: E402
from sora_commander_next import owner_file_review_gate_dev_20261105 as file_gate  # noqa: E402

SOURCE = ROOT / "src" / "sora_commander_next"


class LanguageFileTests(TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(inherited_test_temp()))
        self.env = {"USERPROFILE": str(self.home)}
        self.folder = self.home / ".config" / "sora-commander"

    def write(self, data):
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "language.txt").write_bytes(data)

    def resolve(self, windows="ja"):
        return lang.resolve_owner_ui_language(self.env, windows_language=lambda: windows)

    def test_no_file_follows_windows(self):
        self.assertEqual(self.resolve("ja"), ("ja", "windows"))
        self.assertEqual(self.resolve("en"), ("en", "windows"))

    def test_file_overrides_windows(self):
        self.write(b"en\r\n")
        self.assertEqual(self.resolve("ja"), ("en", "file"))
        self.write(b"\xef\xbb\xbf# my choice\n\n  ja  \n")
        self.assertEqual(self.resolve("en"), ("ja", "file"))

    def test_invalid_file_refuses(self):
        for data in (b"", b"# only a comment\n", b"fr\n", b"EN\n", b"en\nja\n",
                     b"\xff\xfe", b"e" * 300):
            with self.subTest(data=data[:12]):
                self.write(data)
                with self.assertRaises(lang.OwnerLanguageError):
                    self.resolve()

    def test_directory_named_language_txt_refuses(self):
        (self.folder / "language.txt").mkdir(parents=True)
        with self.assertRaises(lang.OwnerLanguageError):
            self.resolve()

    def test_missing_profile_uses_windows_language(self):
        self.assertEqual(lang.resolve_owner_ui_language({}, windows_language=lambda: "en"),
                         ("en", "windows"))


class WindowsLanguageTests(TestCase):
    def test_japanese_ids_map_to_ja_and_others_to_en(self):
        for langid, expected in ((0x0411, "ja"), (0x0409, "en"), (0x0809, "en"),
                                 (0x0804, "en"), (0x0407, "en")):
            with self.subTest(langid=hex(langid)):
                with patch("ctypes.windll") as windll:
                    windll.kernel32.GetUserDefaultUILanguage.return_value = langid
                    self.assertEqual(lang._windows_ui_language(), expected)

    def test_unavailable_api_falls_back_to_english(self):
        with patch("ctypes.windll") as windll:
            windll.kernel32.GetUserDefaultUILanguage.side_effect = OSError
            self.assertEqual(lang._windows_ui_language(), "en")


class CacheTests(TestCase):
    def setUp(self):
        self.enterContext(patch.object(lang, "_cached", None))

    def test_decided_once_per_process(self):
        with patch.object(lang, "resolve_owner_ui_language",
                          side_effect=[("en", "file"), ("ja", "windows")]) as resolve:
            self.assertEqual(lang.owner_ui_language(), "en")
            self.assertEqual(lang.owner_ui_language(), "en")
            self.assertEqual(lang.owner_ui_language_source(), "file")
        resolve.assert_called_once()

    def test_error_is_not_cached_as_a_language(self):
        with patch.object(lang, "resolve_owner_ui_language",
                          side_effect=lang.OwnerLanguageError("bad")):
            with self.assertRaises(lang.OwnerLanguageError):
                lang.owner_ui_language()
        self.assertIsNone(lang._cached)


class WiringTests(TestCase):
    def test_command_window_sends_the_chosen_language(self):
        tree = ast.parse((SOURCE / "owner_approval_broker.py").read_text(encoding="utf-8"))
        sent = [n for n in ast.walk(tree) if isinstance(n, ast.Dict)
                and any(isinstance(k, ast.Constant) and k.value == "locale" for k in n.keys)]
        self.assertEqual(len(sent), 1)
        index = [getattr(k, "value", None) for k in sent[0].keys].index("locale")
        self.assertEqual(ast.unparse(sent[0].values[index]), "owner_ui_language()")
        self.assertNotIn('OWNER_UI_LOCALE="ja"', (SOURCE / "owner_approval_broker.py")
                         .read_text(encoding="utf-8"))

    def test_file_window_defaults_to_the_chosen_language(self):
        for chosen in ("en", "ja"):
            with self.subTest(chosen=chosen), \
                    patch.object(file_gate, "owner_ui_language", return_value=chosen), \
                    patch.object(file_gate, "bound_proposal") as bound:
                screen = bound.return_value.presentation.screen
                for name in ("kind", "title", "heading", "caution", "information", "body"):
                    setattr(screen, name, name)
                bound.return_value.presentation.labels = ("a", "b")
                frame = file_gate.request_frame("0" * 64, object(), 45.0)
                self.assertEqual(bound.call_args.kwargs["locale"], chosen)
                self.assertIn(f'"locale":"{chosen}"'.encode(), frame)

    def test_explicit_file_window_language_still_wins(self):
        with patch.object(file_gate, "owner_ui_language", side_effect=AssertionError), \
                patch.object(file_gate, "bound_proposal") as bound:
            screen = bound.return_value.presentation.screen
            for name in ("kind", "title", "heading", "caution", "information", "body"):
                setattr(screen, name, name)
            bound.return_value.presentation.labels = ("a", "b")
            file_gate.request_frame("0" * 64, object(), 45.0, "en")
            self.assertEqual(bound.call_args.kwargs["locale"], "en")

    def test_broker_and_usual_mode_check_language_at_startup(self):
        broker = (SOURCE / "owner_broker_main.py").read_text(encoding="utf-8")
        self.assertIn("language = owner_ui_language()", broker)
        self.assertIn("[INFO] Window language:", broker)
        usual = (SOURCE / "stdio_main.py").read_text(encoding="utf-8")
        self.assertIn("owner_ui_language()", usual)
        self.assertIn("file=sys.stderr", usual)
