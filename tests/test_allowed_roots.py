"""Startup scope: synthetic environment, bounded real config IO, no Owner state.

The fixture lives under a deliberately forbidden 'temp' ancestor. Virtual Windows
root metadata is mocked separately; name and scope rules are never disabled.
"""
import os
import io
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests._inherited_test_temp import inherited_test_temp
from sora_commander_next import allowed_roots as roots


@unittest.skipUnless(os.name == "nt", "Windows root rules")
class RootRuleTests(unittest.TestCase):
    def setUp(self):
        self.home = Path("C:/Users/Sample")
        self.env = {"USERPROFILE": str(self.home), "LOCALAPPDATA": str(self.home / "AppData/Local"),
                    "SystemDrive": "C:", "SystemRoot": "C:/Windows",
                    "ProgramFiles": "C:/Program Files", "ProgramData": "C:/ProgramData"}
        self.home, self.config, self.blocked, self.contained = roots._locations(self.env)

    def validate(self, value):
        return roots._validate_root(value, 7, self.home, self.blocked, self.contained)

    def test_valid_absolute_folder(self):
        with patch.object(roots, "_plain_path", side_effect=lambda p, **kw: p):
            self.assertEqual(self.validate("C:/Work/Project"), str(Path("C:/Work/Project")))

    def test_forbidden_locations_and_names_never_reach_filesystem(self):
        bad = ("relative/path", "C:relative", "C:/", "//server/share/project",
               "C:/Work/../Project", "C:/Windows/Tasks", "C:/Program Files/App",
               "C:/Program Files (x86)/App", "C:/ProgramData/App", str(self.home),
               str(self.home / "AppData/Local/Project"), "C:/Users", "C:/Work/.hidden",
               "C:/Work/logs/Project", "C:/Work/private.key/Project", "C:/Work/CON",
               "C:/Work/Project:stream")
        with patch.object(roots, "_plain_path") as filesystem:
            for value in bad:
                with self.subTest(value=value), self.assertRaises(roots.AllowedRootsError) as caught:
                    self.validate(value)
                self.assertEqual(caught.exception.line, 7)
                self.assertTrue(str(caught.exception).startswith("allowed-roots line 7: "))
                self.assertNotIn(value, str(caught.exception))
            filesystem.assert_not_called()

    def test_missing_file_link_and_access_denied_are_sanitized(self):
        for error in (FileNotFoundError("private path"), ValueError("linked path"),
                      PermissionError("private path")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(roots, "_plain_path", side_effect=error), \
                    self.assertRaises(roots.AllowedRootsError) as caught:
                self.validate("C:/Work/Project")
            self.assertNotIn("private", str(caught.exception))

    def test_canonical_alias_cannot_enter_system_directory(self):
        with patch.object(roots, "_plain_path", return_value=Path("C:/Program Files/App")), \
                self.assertRaises(roots.AllowedRootsError):
            self.validate("C:/PROGRA~1/App")

    def test_plain_path_rejects_files_and_ancestor_reparse_points(self):
        directory = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0, st_nlink=1)
        reparse = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400, st_nlink=1)
        file = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0, st_nlink=1)
        path = Path("C:/Work/Project")
        for values in ((directory, reparse), (directory, directory, file)):
            with patch.object(Path, "lstat", side_effect=values), self.assertRaises(ValueError):
                roots._plain_path(path, directory=True)

    def test_tests_profile_never_reads_configuration_or_environment(self):
        with patch.object(Path, "resolve", return_value=Path("C:/Checkout")), \
                patch.object(Path, "is_dir", return_value=True), \
                patch.object(roots, "_locations", side_effect=AssertionError("must not read")), \
                patch.object(Path, "open", side_effect=AssertionError("must not open")):
            self.assertEqual(roots.startup_allowed_roots("C:/Checkout", "tests", {}),
                             (str(Path("C:/Checkout/tests")),))


@unittest.skipUnless(os.name == "nt", "Windows inherited fixture")
class RootConfigTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(self.enterContext(inherited_test_temp()))
        self.user = self.scratch / "profile"
        self.config = self.user / ".config/sora-commander/allowed-roots.txt"
        self.config.parent.mkdir(parents=True)
        self.env = dict(os.environ, USERPROFILE=str(self.user),
                        LOCALAPPDATA=str(self.user / "AppData/Local"))
        self.repo = Path("C:/Work/Checkout")
        # Only model existing project directories and the checkout resolve;
        # configuration IO and all policy decisions are real.
        self.real_plain = roots._plain_path
        self.real_resolve = Path.resolve
        self.enterContext(patch.object(Path, "resolve", autospec=True,
            side_effect=lambda p, **kw: p if p == self.repo else self.real_resolve(p, **kw)))
        self.enterContext(patch.object(roots, "_plain_path", side_effect=self.plain))

    def plain(self, path, *, directory):
        return path if directory else self.real_plain(path, directory=False)

    def load(self):
        with patch.dict(os.environ, self.env, clear=True):
            return roots.startup_allowed_roots(self.repo, "development")

    def test_absent_configuration_keeps_checkout_only(self):
        self.assertEqual(self.load(), (str(self.repo),))

    def test_comments_blank_lines_and_one_read_snapshot(self):
        self.config.write_text("# folders\n\nC:/Work/One\n C:/Work/Two \n", encoding="utf-8")
        original = Path.open
        with patch.object(Path, "open", autospec=True,
                          side_effect=lambda p, *a, **kw: original(p, *a, **kw)) as opened:
            snapshot = self.load()
        self.assertEqual(opened.call_count, 1)
        self.config.write_text("C:/Work/Three\n", encoding="utf-8")
        self.assertEqual(snapshot, (str(Path("C:/Work/One")), str(Path("C:/Work/Two"))))

    def test_one_bad_line_refuses_every_entry(self):
        self.config.write_text("# comment\nC:/Work/Good\nrelative-secret-value\n", encoding="utf-8")
        with self.assertRaises(roots.AllowedRootsError) as caught:
            self.load()
        self.assertEqual(caught.exception.line, 3)
        self.assertNotIn("relative-secret-value", str(caught.exception))

    def test_sixteen_entries_allowed_seventeenth_refused(self):
        self.config.write_text("\n".join(f"C:/Work/Project{i}" for i in range(16)), encoding="utf-8")
        self.assertEqual(len(self.load()), 16)
        with self.config.open("a", encoding="utf-8") as stream:
            stream.write("\nC:/Work/Extra\n")
        with self.assertRaises(roots.AllowedRootsError) as caught:
            self.load()
        self.assertEqual(caught.exception.line, 17)

    def test_empty_oversized_and_bad_utf8_refuse_startup(self):
        for data, line in ((b"# no folders\n", 0), (b"x" * (roots.MAX_CONFIG_BYTES + 1), 0),
                           (b"# comment\n\xff", 2)):
            with self.subTest(line=line):
                self.config.write_bytes(data)
                with self.assertRaises(roots.AllowedRootsError) as caught:
                    self.load()
                self.assertEqual(caught.exception.line, line)


class RootCompositionTests(unittest.TestCase):
    def test_stdio_uses_the_same_startup_snapshot_once(self):
        from sora_commander_next import stdio_main
        selected = (str(Path("C:/Work/One")), str(Path("C:/Work/Two")))
        guard = SimpleNamespace(sid="synthetic-owner", verify=Mock())
        with patch.dict(os.environ, {"SORA_NEXT_OWNER_REVIEW_PILOT": "1",
                                     "SORA_NEXT_OWNER_PROFILE": "development"}, clear=True), \
                patch("sora_commander_next.windows_owner.OwnerSessionGuard", return_value=guard), \
                patch("sora_commander_next.windows_owner_controller.WindowsOwnerController"), \
                patch("sora_commander_next.owner_review_dispatch.OwnerReviewDispatcher"), \
                patch.object(stdio_main.Host, "attach_owner_review_local"), \
                patch.object(stdio_main, "startup_allowed_roots", return_value=selected) as loaded, \
                patch.object(stdio_main, "serve_stdio") as serve:
            stdio_main.main()
            host = serve.call_args.args[0]
            try:
                self.assertEqual(host.policy.roots, selected)
                self.assertEqual(loaded.call_count, 1)
            finally:
                host.close()

    def test_broker_startup_prints_count_without_paths(self):
        from sora_commander_next import owner_broker_main as backend
        host = Mock()
        host.jobs._ledger = None
        host.policy.roots = ("private-folder-one", "private-folder-two")
        guard = SimpleNamespace(sid="synthetic-owner", verify=Mock())
        with patch.object(backend, "OwnerSessionGuard", return_value=guard), \
                patch.object(backend, "build_backend_host", return_value=host), \
                patch.object(backend, "OwnerBrokerBackend"), \
                patch.object(backend, "OwnerBrokerService"), \
                patch.object(backend, "OwnerBrokerPipeServer") as server, \
                patch.object(backend.sys, "stdin", []), \
                patch.object(backend.sys, "stdout", new_callable=io.StringIO) as output:
            self.assertEqual(backend.main(["--pipe-name", "synthetic-pipe"]), 0)
            self.assertIn("[INFO] Allowed folders: 2", output.getvalue())
            self.assertNotIn("private-folder", output.getvalue())
            server.return_value.start.assert_called_once()

    def test_backend_and_client_use_the_shared_startup_snapshot(self):
        from sora_commander_next import owner_broker_main as backend
        from sora_commander_next import owner_broker_client_main as client
        selected = (str(Path("C:/Work/One")), str(Path("C:/Work/Two")))
        guard = SimpleNamespace(sid="synthetic-owner")
        env = {"SORA_NEXT_OWNER_PROFILE": "development"}
        with patch.object(backend, "startup_allowed_roots", return_value=selected) as back_roots, \
                patch.object(backend, "WindowsOwnerController"), \
                patch.object(backend, "OwnerReviewDispatcher"), \
                patch.object(backend.Host, "attach_owner_review_local"):
            host = backend.build_backend_host(guard, "C:/Checkout", env)
            try:
                self.assertEqual(host.policy.roots, selected)
            finally:
                host.close()
            back_roots.assert_called_once_with("C:/Checkout", "development", env)
        with patch.object(client, "startup_allowed_roots", return_value=selected) as client_roots, \
                patch.object(client, "OwnerBrokerPipeClient") as pipe, \
                patch.object(client, "DevBrokerHost") as factory:
            client.build_client_host(guard, "C:/Checkout", env, pipe_name="synthetic-pipe")
            self.assertEqual(factory.call_args.kwargs["read_roots"], selected)
            client_roots.assert_called_once_with("C:/Checkout", "development", env)
            pipe.assert_called_once_with(pipe_name="synthetic-pipe")


if __name__ == "__main__":
    unittest.main()
