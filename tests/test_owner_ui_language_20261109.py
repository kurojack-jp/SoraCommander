"""Window language: language.txt first, else the Windows display language."""
import ast
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
import stat
from types import SimpleNamespace
from pathlib import Path
import sys
from unittest import TestCase
from unittest.mock import Mock, patch

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


class DevWriteToolsTests(TestCase):
    def setUp(self):
        self.profile = Path(self.enterContext(inherited_test_temp()))
        self.enterContext(patch.dict(os.environ, USERPROFILE=str(self.profile),
                                     LOCALAPPDATA=str(self.profile / "Local"), clear=True))
        self.config = lang.language_file().with_name("dev-write-tools.txt")
        self.config.parent.mkdir(parents=True)

    def test_usual_and_broker_client_startup_register_only_with_on(self):
        from sora_commander_next import owner_broker_client_main as client, stdio_main
        from sora_commander_next.host import Host
        from sora_commander_next.owner_broker_dev import DevBrokerHost
        from sora_commander_next.mcp_adapter import build_server
        writers = {"send_input", "create_directory", "create_file", "move_file",
                   "request_source_patch_dev"}
        for enabled in (False, True):
            if enabled:
                self.config.write_bytes(b"on\r\n")
            for entry in (stdio_main, client):
                with self.subTest(enabled=enabled, entry=entry.__name__):
                    host = DevBrokerHost(Mock(), read_roots=()) if entry is client else Host()
                    host.task_presets = ()
                    self.addCleanup(host.close)
                    with patch.object(stdio_main, "Host", return_value=host), \
                            patch.object(client, "OwnerSessionGuard"), \
                            patch.object(client, "build_client_host", return_value=host), \
                            patch.object(entry, "serve_stdio") as serve, \
                            patch.object(lang, "resolve_dev_write_tools", wraps=lang.resolve_dev_write_tools) as load, \
                            redirect_stderr(StringIO()):
                        entry.main()
                    load.assert_called_once_with()
                    serve.assert_called_once()
                    sdk = build_server(*serve.call_args.args, **serve.call_args.kwargs)
                    names = {tool.name for tool in sdk._tool_manager.list_tools()}
                    self.assertEqual(names & writers, writers if enabled else set())

    def test_invalid_config_holds_both_clients_and_broker(self):
        from sora_commander_next import owner_broker_client_main as client, owner_broker_main as broker, stdio_main
        args = SimpleNamespace(list_unknown=False, resolve_unknown=None, pipe_name=None)
        for data in (b"", b"off", b"ON", b" on", b"on\n\n", b"on\n# note", b"\xef\xbb\xbfon"):
            with self.subTest(data=data):
                self.config.write_bytes(data)
                expected = "[HOLD] dev-write-tools.txt: must contain exactly one line, on\n"
                for entry in (stdio_main, client):
                    with redirect_stderr(StringIO()) as output, patch.object(entry, "serve_stdio") as serve:
                        with self.assertRaises(SystemExit) as error:
                            entry.main()
                    serve.assert_not_called()
                    self.assertEqual(error.exception.code, 2)
                    self.assertEqual(output.getvalue(), expected)
                with redirect_stdout(StringIO()) as output, patch.object(broker, "build_backend_host") as build:
                    self.assertEqual(broker._run_with_lease(args, None, None), 2)
                build.assert_not_called()
                self.assertEqual(output.getvalue(), expected)

    def test_reparse_point_symlink_and_hard_link_are_refused_before_open(self):
        self.config.write_bytes(b"on")
        for mode, attributes, links in ((stat.S_IFLNK, 0, 1), (stat.S_IFREG, 0x400, 1),
                                         (stat.S_IFREG, 0, 2), (stat.S_IFDIR, 0, 1)):
            with self.subTest(mode=mode, attributes=attributes, links=links):
                info = SimpleNamespace(st_mode=mode, st_file_attributes=attributes, st_nlink=links)
                with patch.object(Path, "lstat", return_value=info), patch.object(Path, "open") as open_file:
                    with self.assertRaisesRegex(lang.DevWriteToolsError, "must be an ordinary file"):
                        lang.resolve_dev_write_tools()
                open_file.assert_not_called()

    def test_broker_prints_the_startup_state(self):
        from sora_commander_next import owner_broker_main as broker
        args = SimpleNamespace(list_unknown=False, resolve_unknown=None, pipe_name="synthetic")
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                if enabled:
                    self.config.write_bytes(b"on\n")
                host = Mock(policy=SimpleNamespace(roots=(), read_only_roots=()),
                            jobs=SimpleNamespace(_ledger=None))
                with patch.object(broker, "build_backend_host", return_value=host), \
                        patch.object(broker, "OwnerBrokerPipeServer") as server, \
                        patch.object(broker, "OwnerBrokerBackend"), \
                        patch.object(broker.sys, "stdin", StringIO()), \
                        patch.object(lang, "owner_ui_language", return_value="en"), \
                        patch.object(lang, "owner_ui_language_source", return_value="windows"), \
                        patch.object(lang, "resolve_dev_write_tools", wraps=lang.resolve_dev_write_tools) as load, \
                        redirect_stdout(StringIO()) as output:
                    self.assertEqual(broker._run_with_lease(args, None, None), 0)
                load.assert_called_once_with()
                server.return_value.start.assert_called_once_with()
                self.assertIn(f"[INFO] DEV write tools: {'on' if enabled else 'off'}",
                              output.getvalue().splitlines())
