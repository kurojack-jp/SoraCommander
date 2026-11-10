"""T1 Windows synthetic Git and SDK contracts; no real network or Owner UI."""
import asyncio
from hashlib import sha256
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from sora_commander_next import git_readonly as git
from sora_commander_next.file_reads import ReadBoundaryError
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import build_server, _validate_arguments

NAMES = ("git_snapshot", "git_diff", "git_log", "git_show", "git_remote_check")


class GitReadTests(unittest.TestCase):
    def setUp(self):
        admission = self.enterContext(canonical_fixture_admission())
        self.enterContext(patch.object(git, "_check", side_effect=admission))
        self.root = Path(self.enterContext(inherited_test_temp())).resolve()
        # Git writes its objects read-only; make them removable before the
        # fixture's own verified cleanup runs (cleanups run last-in first-out).
        self.addCleanup(self._writable)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.roots = (str(self.repo),)
        self.exe = git.fixed_git()
        self.command("init", "-b", "trial")
        self.command("config", "user.name", "Synthetic Author")
        self.command("config", "user.email", "synthetic@example.invalid")
        (self.repo / "hello.txt").write_text("initial\n", encoding="utf-8")
        (self.repo / ".env").write_text("SYNTHETIC_ENV_SECRET\n", encoding="utf-8")
        (self.repo / "key.pem").write_text("SYNTHETIC_KEY_SECRET\n", encoding="utf-8")
        self.command("add", "--", "hello.txt", ".env", "key.pem")
        self.command("commit", "-m", "synthetic initial")
        self.command("config", "branch.trial.remote", "origin")
        self.command("config", "branch.trial.merge", "refs/heads/trial")
        self.command("config", "remote.origin.url", "https://example.invalid/synthetic.git")

    def _writable(self):
        for item in self.root.rglob("*"):
            if item.is_file() and not item.is_symlink():
                os.chmod(item, 0o666)

    def command(self, *args):
        result = subprocess.run([str(self.exe), *git.OPTIONS, *args],
                                cwd=self.repo, env=git.safe_env(self.exe),
                                shell=False, capture_output=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            self.fail("synthetic setup Git failed: " + result.stderr.decode("utf-8", "replace"))
        return result.stdout

    def hashes(self):
        return tuple(sha256((self.repo / ".git" / name).read_bytes()).hexdigest()
                     for name in ("index", "HEAD"))

    def call(self, name, **kwargs):
        before = self.hashes()
        with patch.object(git.subprocess, "Popen", wraps=subprocess.Popen) as launched:
            try:
                return getattr(git, name)(str(self.repo), self.roots, **kwargs)
            finally:
                self.assertEqual(before, self.hashes())
                # Each file-reading command adds one `config` call for filter drivers.
                self.assertLessEqual(launched.call_count, 6)

    def remote(self):
        real = git._run
        def run(cwd, args, **kwargs):
            if args[0] == "ls-remote":
                self.assertTrue(kwargs.get("network"))
                self.assertEqual(args[2], "https://example.invalid/synthetic.git")
                return self.command("rev-parse", "refs/heads/trial").strip() + b"\trefs/heads/trial\n"
            return real(cwd, args, **kwargs)
        with patch.object(git, "_run", side_effect=run):
            return self.call("git_remote_check", ref="refs/heads/trial")

    def test_every_tool_preserves_index_head_and_has_no_review(self):
        (self.repo / "hello.txt").write_text("changed\n", encoding="utf-8")
        self.assertIn("branch.head trial", self.call("git_snapshot")["status"])
        self.assertIn("+changed", self.call("git_diff")["content"])
        self.assertIn("Synthetic Author", self.call("git_log")["content"])
        self.assertIn("+initial", self.call("git_show", rev="HEAD")["content"])
        self.assertTrue(self.remote()["matches"])

    def test_hostile_programs_never_run(self):
        marker = self.root / "executed.txt"
        script = self.root / "program.sh"
        script.write_text("#!/bin/sh\nprintf invoked > '" + marker.as_posix() + "'\n", encoding="utf-8")
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook_names = ("applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit",
                      "pre-merge-commit", "prepare-commit-msg", "commit-msg", "post-commit",
                      "pre-rebase", "post-checkout", "post-merge", "pre-push", "pre-receive",
                      "update", "proc-receive", "post-receive", "post-update", "reference-transaction",
                      "push-to-checkout", "pre-auto-gc", "post-rewrite", "sendemail-validate", "fsmonitor-watchman")
        for name in hook_names:
            hook = hooks / name
            hook.write_bytes(script.read_bytes())
        for key, value in (("core.fsmonitor", str(script)), ("diff.external", str(script)),
                           ("diff.hostile.textconv", str(script)), ("core.pager", str(script)),
                           ("core.hooksPath", str(hooks)), ("alias.status", "!" + str(script)),
                           ("alias.diff", "!" + str(script)), ("log.showSignature", "true"),
                           ("gpg.program", str(script))):
            self.command("config", key, value)
        (self.repo / ".gitattributes").write_text("*.txt diff=hostile\n", encoding="utf-8")
        (self.repo / "hello.txt").write_text("hostile change\n", encoding="utf-8")
        for name, args in (("git_snapshot", {}), ("git_diff", {}), ("git_log", {}),
                           ("git_show", {"rev": "HEAD"}),
                           ("git_show", {"rev": "HEAD", "file": "hello.txt"})):
            self.call(name, **args)
            self.assertFalse(marker.exists(), name)
        self.remote()
        self.assertFalse(marker.exists())

    def test_filter_drivers_never_run(self):
        # Claude 2026-11-10: clean/smudge/process filters named by .gitattributes
        # or .git/info/attributes ran during status/diff before the overrides.
        marker = self.root / "filtered.txt"
        script = self.root / "filter.sh"
        script.write_text("#!/bin/sh\nprintf invoked > '" + marker.as_posix() + "'\ncat\n",
                          encoding="utf-8")
        for driver in ("hostile", "info.driver"):
            for variable in ("clean", "smudge", "process"):
                self.command("config", f"filter.{driver}.{variable}", str(script))
            self.command("config", f"filter.{driver}.required", "true")
        (self.repo / ".gitattributes").write_text("*.txt filter=hostile\n", encoding="utf-8")
        info = self.repo / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text("hello.txt filter=info.driver\n", encoding="utf-8")
        (self.repo / "hello.txt").write_text("filtered change\n", encoding="utf-8")
        for name, args in (("git_snapshot", {}), ("git_diff", {}), ("git_diff", {"stat_only": True}),
                           ("git_log", {}), ("git_show", {"rev": "HEAD"}),
                           ("git_show", {"rev": "HEAD", "file": "hello.txt"})):
            self.call(name, **args)
            self.assertFalse(marker.exists(), name)

    def test_filter_driver_name_with_equals_is_refused(self):
        self.command("config", "filter.a=b.clean", "x")
        with self.assertRaises(ReadBoundaryError):
            self.call("git_snapshot")

    def test_remote_check_accepts_a_short_branch_name(self):
        real = git._run
        def run(cwd, args, **kwargs):
            if args[0] == "ls-remote":
                self.assertEqual(args[-1], "refs/heads/trial")
                return self.command("rev-parse", "refs/heads/trial").strip() + b"\trefs/heads/trial\n"
            return real(cwd, args, **kwargs)
        with patch.object(git, "_run", side_effect=run):
            self.assertTrue(self.call("git_remote_check", ref="trial")["matches"])

    def test_protected_diff_and_historical_file(self):
        for name in (".env", "key.pem"):
            (self.repo / name).write_text("MORE_SYNTHETIC_SECRET\n", encoding="utf-8")
        for result in (self.call("git_diff"), self.call("git_show", rev="HEAD")):
            self.assertNotIn("SECRET", result["content"])
            self.assertIn("Protected file content omitted.", result["content"])
        for name in (".env", "key.pem", "credentials/password.txt"):
            with patch.object(git.subprocess, "Popen") as process:
                with self.assertRaises(ReadBoundaryError):
                    self.call("git_show", rev="HEAD", file=name)
                process.assert_not_called()

    def test_large_diff_and_show_pages_join(self):
        text = "".join(f"{i:06d} 日本語 synthetic line\n" for i in range(6000))
        # newline="\n": the stored blob must equal `text` on Windows too.
        (self.repo / "hello.txt").write_text(text, encoding="utf-8", newline="\n")
        for name, args, expected in (
                ("git_diff", {}, self.command("diff", *git.DIFF_OPTIONS, "HEAD", "--patch", "--", "hello.txt").decode()),
                ("git_show", {"rev": "HEAD", "file": "hello.txt"}, "initial\n")):
            if name == "git_show":
                self.command("add", "--", "hello.txt")
                self.command("commit", "-m", "large synthetic")
                expected = text
            offset, pages = 0, []
            while True:
                result = self.call(name, offset=offset, **args)
                self.assertLessEqual(len(result["content"].encode("utf-8")), git.CAP)
                pages.append(result["content"])
                if result["next_offset"] is None:
                    break
                self.assertGreater(result["next_offset"], offset)
                offset = result["next_offset"]
            self.assertEqual("".join(pages), expected)
            self.assertGreater(len(pages), 1)

    def test_paths_and_base_stat(self):
        (self.repo / "hello.txt").write_text("changed\n", encoding="utf-8")
        self.assertIn("hello.txt", self.call("git_diff", paths=["hello.txt"], stat_only=True)["content"])
        self.assertEqual(self.call("git_diff", base="HEAD")["content"], "")
        self.assertIn("synthetic initial", self.call("git_log", paths=["hello.txt"], limit=1)["content"])

    def test_untracked_count_opt_in(self):
        (self.repo / "untracked.txt").write_text("fake", encoding="utf-8")
        self.assertEqual(self.call("git_snapshot")["untracked_count"], 1)
        self.assertEqual(self.call("git_snapshot")["untracked"], [])
        self.assertEqual(self.call("git_snapshot", include_untracked=True)["untracked"], ["untracked.txt"])

    def test_roots_and_top_above_root(self):
        with patch.object(git.subprocess, "Popen") as process:
            with self.assertRaises(ReadBoundaryError):
                git.git_snapshot(str(self.root), self.roots)
            process.assert_not_called()
        child = self.repo / "child"
        child.mkdir()
        with self.assertRaises(ReadBoundaryError):
            git.git_snapshot(str(child), (str(child),))
        self.assertIn("branch.head", git.git_snapshot(str(child), self.roots)["status"])

    def test_link_to_outside_refused(self):
        # Model a junction at the same OS boundary without creating an artifact
        # the inherited fixture's cleanup must refuse. Actual junction not made.
        original = Path.is_junction
        with patch.object(Path, "is_junction", lambda p: p == self.repo or original(p)):
            with patch.object(git.subprocess, "Popen") as process:
                with self.assertRaises(ReadBoundaryError):
                    self.call("git_snapshot")
                process.assert_not_called()

    def test_non_https_no_network(self):
        for url in ("ssh://example.invalid/r", "file:///outside", "ext::anything", "git@example.invalid:r"):
            self.command("config", "remote.origin.url", url)
            with patch.object(git.subprocess, "Popen", wraps=subprocess.Popen) as process:
                with self.assertRaises(ReadBoundaryError):
                    self.call("git_remote_check", ref="refs/heads/trial")
                self.assertFalse(any("ls-remote" in c.args[0] for c in process.call_args_list))

    def test_sdk_returns_fixed_reason_codes(self):
        # Claude 2026-11-10: ChatGPT only saw "Tool request failed" before.
        host = Host(policy=Policy(read_only_roots=self.roots))
        self.addCleanup(host.jobs.close)
        sdk = build_server(host)
        call = lambda name, args: asyncio.run(sdk.call_tool(name, {"path": str(self.repo), **args}))[1]
        self.command("config", "--unset", "branch.trial.remote")
        self.assertEqual(call("git_remote_check", {"ref": "trial"}),
                         {"status": "ERROR", "error": "NO_UPSTREAM"})
        self.command("config", "branch.trial.remote", "origin")
        real = git._run
        def offline(cwd, args, **kwargs):
            if args[0] == "ls-remote":
                raise ReadBoundaryError("REMOTE_READ_FAILED")
            return real(cwd, args, **kwargs)
        with patch.object(git, "_run", side_effect=offline):
            self.assertEqual(call("git_remote_check", {"ref": "trial"}),
                             {"status": "ERROR", "error": "REMOTE_READ_FAILED"})
        self.assertEqual(call("git_show", {"rev": "HEAD", "file": ".env"}),
                         {"status": "ERROR", "error": "PROTECTED_FILE"})
        # A read-boundary refusal never echoes its text or the path.
        outside = asyncio.run(sdk.call_tool("git_snapshot", {"path": str(self.root)}))[1]
        self.assertEqual(outside, {"status": "ERROR", "error": "PATH_NOT_ALLOWED"})

    def test_network_failure_code_only_for_ls_remote(self):
        with patch.object(git.subprocess, "Popen") as popen:
            process = popen.return_value.__enter__.return_value
            process.stdout.read.return_value = b""
            process.wait.return_value = 128
            with self.assertRaisesRegex(ReadBoundaryError, "^REMOTE_READ_FAILED$"):
                git._run(self.repo, ["ls-remote", "x"], network=True)
            with self.assertRaisesRegex(ReadBoundaryError, "^GIT_FAILED$"):
                git._run(self.repo, ["status"])

    def test_registration_schema_client_host_read_only_roots(self):
        host = Host(policy=Policy(read_only_roots=self.roots))
        self.addCleanup(host.jobs.close)
        sdk = build_server(host)
        tools = {t.name: t for t in sdk._tool_manager.list_tools()}
        expected = {"git_snapshot": {"path", "include_untracked"},
                    "git_diff": {"path", "base", "paths", "stat_only", "offset"},
                    "git_log": {"path", "limit", "paths"},
                    "git_show": {"path", "rev", "file", "offset"},
                    "git_remote_check": {"path", "ref"}}
        for name in NAMES:
            self.assertEqual(set(tools[name].parameters["properties"]), expected[name])
            self.assertIn("No approval window", tools[name].description)
            self.assertIn("hooks/filters", tools[name].description)
        _, result = asyncio.run(sdk.call_tool("git_snapshot", {"path": str(self.repo)}))
        self.assertIn("branch.head", result["status"])
        # Broker adapter metadata and dispatch use the client policy, no backend.
        from sora_commander_next.broker_host import BrokerHost
        broker = object.__new__(BrokerHost)
        broker.policy = host.policy
        broker_sdk = build_server(broker)
        _, result = asyncio.run(broker_sdk.call_tool("git_log", {"path": str(self.repo)}))
        self.assertIn("Synthetic Author", result["content"])


class GitValidationTests(unittest.TestCase):
    def test_invalid_values_before_any_process(self):
        bad_revs = ("-n1", "HEAD..main", "HEAD:file", "x\ny", "x\0y", "a" * 201, "", None, 4)
        bad_paths = ("-a", "../a", "a/../b", "x:y", "x\ny", "x\0y", "/absolute", "C:\\absolute", "\\rooted", "a" * 4097)
        cases = []
        for value in bad_revs:
            cases.extend((("git_show", {"rev": value}), ("git_remote_check", {"ref": value})))
            if value is not None:
                cases.append(("git_diff", {"base": value}))
        for value in bad_paths:
            cases.extend((("git_diff", {"paths": [value]}), ("git_log", {"paths": [value]}),
                          ("git_show", {"rev": "HEAD", "file": value})))
        cases.extend((("git_diff", {"paths": ["a"] * 33}), ("git_log", {"limit": 201}),
                      ("git_log", {"limit": 0}), ("git_log", {"limit": True}),
                      ("git_diff", {"offset": -1}), ("git_diff", {"stat_only": 1}),
                      ("git_snapshot", {"include_untracked": "yes"})))
        with patch.object(git.subprocess, "Popen") as process:
            for name, kwargs in cases:
                with self.subTest(name=name, kwargs=kwargs):
                    with self.assertRaises(ReadBoundaryError):
                        getattr(git, name)("D:/synthetic", (), **kwargs)
                    with self.assertRaises(ValueError):
                        _validate_arguments(name, {"path": "D:/synthetic", **kwargs})
            process.assert_not_called()

    def test_missing_fixed_git_no_process(self):
        git.fixed_git.cache_clear()
        self.addCleanup(git.fixed_git.cache_clear)
        with patch.dict(os.environ, {"ProgramFiles": "D:/nonexistent-git-install", "ProgramW6432": ""}), patch.object(git.subprocess, "Popen") as process:
            with self.assertRaisesRegex(ReadBoundaryError, "GIT_NOT_INSTALLED"):
                git._run(Path.cwd(), ["status"])
            process.assert_not_called()

    def test_env_ignores_injected_git_variables(self):
        with patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor",
                                    "GIT_CONFIG_VALUE_0": "hostile", "GIT_EXTERNAL_DIFF": "hostile",
                                    "GIT_SSH_COMMAND": "hostile", "GIT_DIR": "outside", "PATH": "hostile"}):
            env = git.safe_env(Path("C:/Program Files/Git/cmd/git.exe"))
        for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_EXTERNAL_DIFF", "GIT_SSH_COMMAND", "GIT_DIR"):
            self.assertNotIn(key, env)
        self.assertNotIn("hostile", env["PATH"])
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(env["GIT_ASKPASS"], "")
        self.assertEqual(env["SSH_ASKPASS"], "")

    def test_timeouts_no_shell_no_window_and_sanitized_errors(self):
        process = Mock()
        process.stdout.read.return_value = b""
        process.wait.side_effect = [subprocess.TimeoutExpired("private", 15), 0]
        process.__enter__ = Mock(return_value=process)
        process.__exit__ = Mock(return_value=False)
        with patch.object(git.subprocess, "Popen", return_value=process) as launch:
            with self.assertRaisesRegex(ReadBoundaryError, "^GIT_TIMEOUT$"):
                git._run(Path.cwd(), ["status"])
            self.assertFalse(launch.call_args.kwargs["shell"])
            self.assertEqual(launch.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
            self.assertEqual(process.wait.call_args_list[0].kwargs["timeout"], 15)
        process.wait.side_effect = [subprocess.TimeoutExpired("private", 20), 0]
        with patch.object(git.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(ReadBoundaryError, "GIT_TIMEOUT"):
                git._run(Path.cwd(), ["ls-remote"], network=True)
            self.assertEqual(process.wait.call_args_list[-2].kwargs["timeout"], 20)
        host = Host()
        self.addCleanup(host.jobs.close)
        server = build_server(host)
        from mcp.server.fastmcp.exceptions import ToolError
        with patch.object(git, "git_snapshot", side_effect=OSError("PRIVATE_ENV_OUTSIDE_PATH")):
            with self.assertRaises(ToolError) as error:
                asyncio.run(server.call_tool("git_snapshot", {"path": "D:/synthetic"}))
            self.assertNotIn("PRIVATE_ENV_OUTSIDE_PATH", str(error.exception))

    def test_fixed_git_absolute_file_and_fallback(self):
        git.fixed_git.cache_clear()
        self.addCleanup(git.fixed_git.cache_clear)
        actual = git.fixed_git()
        self.assertTrue(actual.is_absolute())
        self.assertTrue(actual.is_file())
        self.assertFalse(actual.is_symlink())
        git.fixed_git.cache_clear()
        with patch.dict(os.environ, {"ProgramFiles": "D:/missing", "ProgramW6432": str(actual.parents[2])}):
            self.assertEqual(git.fixed_git(), actual)
        git.fixed_git.cache_clear()
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(ReadBoundaryError, "GIT_INSTALL_LINK"):
                git.fixed_git()


if __name__ == "__main__":
    unittest.main()
