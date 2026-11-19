"""Guarded Muse bridge inbox tests with synthetic profiles only."""

from contextlib import nullcontext, redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
import stat
import sys
import unittest
from unittest.mock import patch

from sora_commander_next.activity import ActivityStore, InvalidMetadata, Record
from sora_commander_next.allowed_roots import AllowedRoots
from sora_commander_next.mcp_adapter import build_server
from tests._inherited_test_temp import inherited_test_temp
from tools import scn_muse_bridge as bridge
from tools.scn_muse_inbox import Inbox


ROOT = Path(__file__).resolve().parents[1]
ARGV = ["--checkout", str(ROOT), "--bind", "100.64.0.1"]
_LSTAT = Path.lstat


class BridgeTests(unittest.TestCase):
    def test_parser_errors(self):
        for args in (["--auth", "token"], ["--mode", "exec"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as result:
                    bridge.main(ARGV + args + ["--inbox"])
                self.assertEqual(result.exception.code, 2)
                self.assertIn("--inbox requires", errors.getvalue())

    def test_real_readonly_build_server_tool_counts(self):
        # Mock filesystem setup and listener startup, using real bridge tool registration.
        profile = ROOT / "synthetic-inbox-profile"
        config = profile / ".config" / "sora-commander" / "allowed-roots.txt"
        counts = []

        def capture(host, **options):
            server = build_server(host, **options)
            servers.append(server)
            return server

        servers = []
        with (patch.dict(os.environ, USERPROFILE=str(profile)),
              patch.object(sys, "path", list(sys.path)),
              patch.object(Path, "lstat", autospec=True) as lstat,
              patch.object(bridge, "load_peers", return_value={"readonly": ["fake-node"]}),
              patch("sora_commander_next.allowed_roots._locations",
                    return_value=(profile, config, (), ())),
              patch("sora_commander_next.allowed_roots.startup_allowed_roots",
                    return_value=AllowedRoots((), (str(ROOT),))),
              patch("sora_commander_next.owner_journal_lease.owner_journal_lease",
                    return_value=nullcontext()),
              patch("sora_commander_next.mcp_adapter.build_server", side_effect=capture),
              patch("tools.scn_muse_inbox._check_folder"),
              patch.object(bridge, "transport_lists", return_value=([], [])),
              patch("uvicorn.run") as listen,
              redirect_stdout(io.StringIO()) as output):
            lstat.side_effect = lambda path: None if path == config else _LSTAT(path)
            for enabled in (False, True):
                self.assertEqual(bridge.main(ARGV + (["--inbox"] if enabled else [])), 0)
                names = {tool.name for tool in servers[-1]._tool_manager.list_tools()}
                self.assertIn("bridge_digest", names)
                self.assertEqual("inbox_drop" in names, enabled)
                counts.append(len(names))
            self.assertEqual(listen.call_count, 2)
            self.assertEqual(output.getvalue().count(
                "[bridge] read-only, plus one write-only inbox folder."), 1)
        self.assertEqual(counts, [42, 43])
        print("MUSE_INBOX_READONLY_TOOL_COUNTS", *counts, flush=True)

    def test_activity_validation(self):
        row = Record(operation_type="inbox_drop", state="CREATED", state_source="inbox",
                     pending_id=hashlib.sha256(b"note.md").hexdigest()[:32])
        self.assertEqual(row.data()["state"], "CREATED")
        for changes in ({"operation_type": "command"}, {"job_state": "RUNNING"},
                        {"review_state": "REVIEW"}, {"state_source": "activity_only"},
                        {"request_id": "a" * 32}, {"session_id": "a" * 48},
                        {"pending_id": None}, {"pending_id": "a" * 48}):
            with self.subTest(changes=changes), self.assertRaises(InvalidMetadata):
                replace(row, **changes).data()


class InboxFilesystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(cls.enterClassContext(inherited_test_temp()))

    def setUp(self):
        profile = self.scratch / self._testMethodName
        (profile / ".config" / "sora-commander").mkdir(parents=True)
        self.enterContext(patch.dict(os.environ, USERPROFILE=str(profile)))
        self.output = self.enterContext(redirect_stdout(io.StringIO()))
        self.store = ActivityStore()
        self.inbox = Inbox(os.environ["USERPROFILE"], self.store)
        self.folder = self.inbox.folder

    def assert_refused(self, reason, filename="note.md", content="test"):
        before = set(self.folder.iterdir())
        self.output.seek(0)
        self.output.truncate()
        self.assertEqual(self.inbox.drop(filename, content), {"status": "REFUSED", "reason": reason})
        self.assertEqual(set(self.folder.iterdir()), before)
        self.assertEqual(self.output.getvalue(), f"[bridge] inbox refused: {reason}\n")
        self.assertEqual(self.store.snapshot(), [])

    def test_invalid_name_and_content(self):
        for name in ("../x.md", r"a\b.md", "CON.txt", "x.md.", "x" * 98 + ".md"):
            with self.subTest(name=name):
                self.assert_refused("NAME", name)
        self.assert_refused("EXTENSION", "x.exe")
        self.assert_refused("TOO_LARGE", content="é" * (128 * 1024 + 1))
        self.assert_refused("NOT_UTF8", content="\ud800")
        self.assert_refused("NOT_UTF8", content="a\0b")

    def test_activity_tool_reports_drop_as_created(self):
        # Review fix: the activity observer must not project a finished drop
        # as OUTCOME_UNKNOWN (which would ask for manual reconciliation).
        from types import SimpleNamespace
        from sora_commander_next.activity import observe
        self.assertEqual(self.inbox.drop("seen.md", "x")["status"], "CREATED")
        host = SimpleNamespace(_activity_store=self.store, _owner_review_dispatcher=None,
                               approval=SimpleNamespace(activity_snapshot=lambda: []),
                               jobs=None, _dev_interactive_jobs=None)
        reply = observe(host, {"mode": "recent"})
        self.assertEqual(reply["status"], "OK")
        [row] = reply["records"]
        self.assertEqual((row["operation_type"], row["state"], row["state_source"]),
                         ("inbox_drop", "CREATED", "inbox"))
        self.assertEqual((row["required_action"], row["next_tool"]), ("REPORT_AND_WAIT", None))

    def test_drop_through_mcp_wire(self):
        # Live-check fix: Muse got "Invalid tool request" because the adapter's
        # wire validation had no entry for inbox_drop. Call it as a client does.
        import asyncio
        from mcp.server.fastmcp.exceptions import ToolError
        from sora_commander_next.host import Host
        from tools.scn_muse_inbox import register_inbox
        host = Host()
        self.addCleanup(host.jobs.close)
        server = build_server(host, dev_writes_enabled=False, dev_tools_enabled=False)
        register_inbox(server, self.store, os.environ["USERPROFILE"])
        _, result = asyncio.run(server.call_tool(
            "inbox_drop", {"filename": "wire.md", "content": "inbox test"}))
        self.assertEqual(result["status"], "CREATED")
        self.assertEqual((self.folder / "wire.md").read_bytes(), b"inbox test")
        _, result = asyncio.run(server.call_tool(
            "inbox_drop", {"filename": "wire.md", "content": "inbox test"}))
        self.assertEqual(result, {"status": "REFUSED", "reason": "EXISTS"})
        for arguments in ({"filename": "x.md"}, {"filename": "x.md", "content": 1},
                          {"filename": 1, "content": "x"},
                          {"filename": "x.md", "content": "x", "extra": 1}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                asyncio.run(server.call_tool("inbox_drop", arguments))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["wire.md"])

    def test_success(self):
        content = "受信メモ"
        filename = "note.MD"
        data = content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(self.inbox.drop(filename, content),
                         {"status": "CREATED", "filename": filename,
                          "bytes": len(data), "sha256": digest})
        final = self.folder / filename
        self.assertEqual(list(self.folder.iterdir()), [final])
        self.assertEqual(final.read_bytes(), data)
        records = self.store.snapshot()
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]["operation_type"], records[0]["state_source"], records[0]["state"]),
                         ("inbox_drop", "inbox", "CREATED"))
        self.assertEqual(records[0]["pending_id"], hashlib.sha256(filename.encode()).hexdigest()[:32])
        self.assertRegex(self.output.getvalue(),
                         rf"^\[bridge\] inbox drop: \S+Z note\.MD {len(data)} {digest}\n\Z")

    def test_existing_file(self):
        final = self.folder / "note.md"
        final.write_bytes(b"original")
        self.assert_refused("EXISTS")
        self.assertEqual(final.read_bytes(), b"original")

    def test_count_cap(self):
        for number in range(200):
            (self.folder / f"{number}.txt").touch(exist_ok=False)
        self.assert_refused("INBOX_FULL")

    def test_size_cap(self):
        with (self.folder / "large.txt").open("xb") as stream:
            stream.truncate(32 * 1024 * 1024)
        self.assert_refused("INBOX_FULL")

    def test_linked_folder(self):
        linked = os.stat_result((stat.S_IFLNK, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        with patch.object(Path, "lstat", autospec=True,
                          side_effect=lambda path: linked if path == self.folder else _LSTAT(path)):
            self.assert_refused("LINKED_FOLDER")

    def test_write_failed_removes_temp(self):
        with patch("tools.scn_muse_inbox.os.fsync", side_effect=OSError("synthetic failure")):
            self.assert_refused("WRITE_FAILED")
