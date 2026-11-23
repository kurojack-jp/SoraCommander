"""Offline Muse digest calls through the installed MCP SDK."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from sora_commander_next import activity, git_readonly as git
from sora_commander_next.file_reads import ReadBoundaryError
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import build_server
from sora_commander_next.policy import Policy
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from tools import scn_muse_bridge as bridge
from tools.scn_muse_digest import register_digest


_CHECKOUT = Path(__file__).resolve().parents[1]
_DATE = "2026-11-18T00:00:00+00:00"
_SHA = "a" * 40


async def _call(server, name, **arguments):
    _, result = await server.call_tool(name, arguments)
    return result


class DigestTests(unittest.IsolatedAsyncioTestCase):
    def build_digest(self, checkout=_CHECKOUT, roots=()):
        host = Host(policy=Policy(roots=(), read_only_roots=roots))
        self.addCleanup(host.close)
        server = build_server(host, dev_writes_enabled=False, dev_tools_enabled=False,
                              command_tools_enabled=False)
        register_digest(server, host, checkout=checkout, auth="both", inbox=False)
        return server

    async def test_wire_arguments_and_activity_agreement(self):
        server = self.build_digest()
        original = activity.observe
        with patch.object(activity, "observe", wraps=original) as observe:
            default = await _call(server, "bridge_digest")
            self.assertEqual(default["status"]["registered_tool_count"], 36)
            self.assertEqual(list(default), ["status", "git", "activity", "truncated"])
            self.assertEqual(observe.call_args.args[1], {"mode": "recent", "limit": 5})
            observe.reset_mock()
            self.assertEqual((await _call(server, "bridge_digest", activity_limit=0))["activity"], [])
            observe.assert_not_called()
        for limit in (5, 20):
            with self.subTest(limit=limit):
                digest = await _call(server, "bridge_digest", activity_limit=limit)
                recent = await _call(server, "activity", mode="recent", limit=limit)
                self.assertEqual(recent["status"], "OK")
                self.assertEqual(digest["activity"], recent["records"])
        for arguments in ({"activity_limit": value} for value in (21, -1, True, "5", None)):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                await _call(server, "bridge_digest", **arguments)
        with self.assertRaises(ToolError):
            await _call(server, "bridge_digest", extra=5)

    async def test_git_errors_and_activity_errors_keep_remaining_sections(self):
        server = self.build_digest()
        for failure, code in ((ReadBoundaryError("GIT_TIMEOUT"), "GIT_TIMEOUT"),
                              (ReadBoundaryError("sensitive path excluded"), "PATH_NOT_ALLOWED"),
                              (RuntimeError("synthetic private path"), "GIT_FAILED")):
            with self.subTest(code=code), patch.object(git, "git_snapshot", side_effect=failure), \
                    patch.object(activity, "observe", return_value={"status": "ERROR"}):
                digest = await _call(server, "bridge_digest")
                self.assertEqual(digest["git"], {"error": code})
                self.assertEqual(digest["activity"], {"error": "UNAVAILABLE"})
                self.assertFalse(digest["status"]["remote_ready"])

    async def test_cap_shortens_subjects_before_dropping_oldest_records(self):
        server = self.build_digest()
        subject = "集" * 1800
        records = [{"sequence": index, "payload": "記" * 400} for index in range(20)]
        snapshot = {"status": f"# branch.oid {_SHA}\n# branch.head trial", "untracked_count": 0}
        log = {"content": f"{_SHA}\tSynthetic\tAuthor\t{_DATE}\t{subject}\tend\n" * 3}
        with patch.object(git, "git_snapshot", return_value=snapshot), \
                patch.object(git, "git_log", return_value=log), \
                patch.object(activity, "observe") as observe:
            observe.return_value = {"status": "OK", "records": records[:4]}
            shortened = await _call(server, "bridge_digest")
            observe.return_value = {"status": "OK", "records": records}
            reduced = await _call(server, "bridge_digest", activity_limit=20)
            snapshot["status"] = f"# branch.oid {_SHA}\n# branch.head {'長' * 6000}"
            long_ref = await _call(server, "bridge_digest", activity_limit=0)
        for digest in (shortened, reduced, long_ref):
            self.assertTrue(digest["truncated"])
            self.assertEqual([item["subject"] for item in digest["git"]["log"]], [subject[:80]] * 3)
            self.assertLessEqual(len(json.dumps(digest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")), 16 * 1024)
        self.assertEqual(shortened["activity"], records[:4])
        count = len(reduced["activity"])
        self.assertGreater(count, 0)
        self.assertLess(count, 20)
        self.assertEqual(reduced["activity"], records[:count])

    async def test_synthetic_git_repository_agreement_and_read_boundary(self):
        with inherited_test_temp() as folder, canonical_fixture_admission() as admit:
            repo = Path(folder) / "digest-repo"
            repo.mkdir()
            await self.check_repository(repo, admit)

    async def check_repository(self, repo, admit):
        outside = repo.parent / "outside"
        outside.mkdir()
        commits = [(str(number) * 40, f"Synthetic digest commit {number}")
                   for number in (4, 3, 2)]
        replies = {
            ("rev-parse", "--show-toplevel"): str(repo) + "\n",
            ("status", "--porcelain=v2", "--branch", "--untracked-files=all"):
                f"# branch.oid {commits[0][0]}\n# branch.head digest-trial\n"
                f"1 .M N... 100644 100644 100644 {_SHA} {_SHA} note.txt\n? untracked.txt\n",
            ("log", *git.DIFF_OPTIONS, "--oneline", "-n", "10"):
                "".join(f"{sha[:7]} {subject}\n" for sha, subject in commits),
            ("log", *git.DIFF_OPTIONS, "-n3", "--format=%H%x09%an%x09%ad%x09%s", "--date=iso-strict", "--"):
                "".join(f"{sha}\tSynthetic Author\t{_DATE}\t{subject}\n" for sha, subject in commits),
        }

        def run(cwd, args):
            self.assertEqual(cwd, repo)
            return replies[tuple(args)].encode("utf-8")

        with patch.object(git, "_check", side_effect=admit), patch.object(git, "_run", side_effect=run) as commands:
            server = self.build_digest(repo, (str(repo),))
            digest = await _call(server, "bridge_digest")
            status = await _call(server, "status")
            catalog = await _call(server, "tool_catalog")
            self.assertEqual(digest["status"], {
                "version": catalog["installed_distribution_version"], "mode": "readonly",
                "auth": "both", "inbox": False, "registered_tool_count": status["registered_tool_count"],
                "tool_schema_sha256": status["tool_schema_sha256"], "remote_ready": status["remote_ready"],
            })
            snapshot = await _call(server, "git_snapshot", path=str(repo))
            log = await _call(server, "git_log", path=str(repo), limit=3)
            self.assertIn("# branch.head " + digest["git"]["branch"], snapshot["status"].splitlines())
            self.assertIn("# branch.oid " + digest["git"]["head"], snapshot["status"].splitlines())
            self.assertEqual(digest["git"]["changed"], 1)
            self.assertEqual(digest["git"]["untracked"], snapshot["untracked_count"])
            self.assertEqual(digest["git"]["untracked"], 1)
            expected = [line.split("\t", 3) for line in log["content"].splitlines()]
            self.assertEqual(len(expected), 3)
            self.assertEqual(digest["git"]["log"], [
                {"sha": sha, "date": date, "subject": subject} for sha, _, date, subject in expected
            ])
            commands.reset_mock()
            refused = await _call(self.build_digest(outside, (str(repo),)), "bridge_digest")
            self.assertEqual(refused["git"], {"error": "CHECKOUT_NOT_IN_READ_ROOTS"})
            self.assertEqual(refused["status"], digest["status"])
            self.assertEqual(refused["activity"], digest["activity"])
            commands.assert_not_called()
            print("MUSE_DIGEST_SYNTHETIC_REPLY", json.dumps(digest, ensure_ascii=False), flush=True)


class ExecRegistrationTests(unittest.TestCase):
    def test_exec_bridge_has_no_digest(self):
        host = Host(policy=Policy(roots=()))
        args = SimpleNamespace(mode="exec", inbox=False, enable_edit_pilot=False,
                               enable_create_pilot=False, enable_dev_writes=False,
                               bind="100.64.0.1", port=8765, allow_magicdns=False)
        server = build_server(host, dev_writes_enabled=False)
        with (patch.object(bridge, "build_exec_host", return_value=host),
              patch("sora_commander_next.mcp_adapter.build_server", return_value=server),
              patch.object(bridge, "transport_lists", return_value=([], [])),
              patch("uvicorn.run") as listen, redirect_stdout(io.StringIO())):
            self.assertEqual(bridge._serve_bridge(args, _CHECKOUT, "synthetic", "both", object()), 0)
        listen.assert_called_once()
        self.assertNotIn("bridge_digest", {tool.name for tool in server._tool_manager.list_tools()})
