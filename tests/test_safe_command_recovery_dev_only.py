"""Safe full-command recovery on disposable files, NEVER real Owner archives.

Verifies byte-preserving native command identities, fail-closed selection,
authenticated MCP reads, legacy archive compatibility and no automatic replay.
"""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.jobs import JobManager
from sora_commander_next.mcp_adapter import build_server
from sora_commander_next.offline_result_archive import (
    ResultArchiveError, read_previous_results, write_result_checkpoint,
)
from sora_commander_next.safe_command_identity import (
    display_safe_command, safe_command_identity,
)


class SafeCommandRecoveryTests(unittest.TestCase):
    def test_only_fixed_literal_nonsensitive_commands_identified(self):
        exe, cwd = sys.executable, str(Path.cwd())
        good = safe_command_identity(exe, ["--version"], cwd)
        self.assertIsNotNone(good)
        self.assertEqual(good["executable"], exe)
        self.assertEqual(good["argv"], ["--version"])
        self.assertEqual(good["cwd"], cwd)
        self.assertIn("--version", display_safe_command(good))
        self.assertIsNone(safe_command_identity(exe,
            ["-c", 'token = "FAKE-PRIVATE-VALUE-DO-NOT-STORE"'], cwd))
        self.assertIsNone(safe_command_identity(exe,
            ["--password", "FAKE-PRIVATE-VALUE"], cwd))
        self.assertIsNone(safe_command_identity(exe, ["--version"],
            str(Path.cwd() / "secret")))
        self.assertIsNone(safe_command_identity(exe,
            ["--version"], str(Path.cwd() / "token-store")))
        self.assertIsNone(safe_command_identity(exe, ["-m", "pip"], cwd))
        self.assertIsNone(display_safe_command({
            "executable": exe, "argv": ["--version"], "cwd": cwd,
            "token": "FORGED"}))

    def _one_job(self, temp, argv, rid):
        home = Path(temp)
        journal = home / "owner-session-journal.json"
        archive = home / "owner-output-archive.json"
        manager = JobManager(journal_path=journal, output_archive_path=archive)
        cmd = Command(sys.executable, tuple(argv), str(home),
                      timeout=4, request_id=rid)
        try:
            started = manager.start(cmd)
            self.assertEqual(started["status"], "RUNNING")
            finished = manager.wait(started["session_id"])
            self.assertEqual(finished["status"], "COMPLETED", finished)
            with manager._lock:
                thread = manager._jobs[started["session_id"]].thread
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        finally:
            manager.close()
        return journal, archive, started["session_id"]

    def test_safe_full_command_available_only_after_owner_verification(self):
        with tempfile.TemporaryDirectory(prefix="sora-safe-cmd-") as d:
            journal, archive, sid = self._one_job(d, ("--version",), "a" * 32)
            archived = read_previous_results(archive)["results"][0]
            self.assertEqual(archived["safe_command"]["argv"], ["--version"])
            self.assertEqual(archived["safe_command"]["cwd"], d)
            reopened = JobManager(journal_path=journal, output_archive_path=archive)
            try:
                host = Host(jobs=reopened)
                denied = host.previous_output(sid)
                self.assertEqual(denied["status"], "DENY")
                self.assertNotIn("safe_command", denied)
                hidden = host.recovery_overview()["sessions"][0]
                self.assertFalse(hidden["command_identified"])
                self.assertNotIn("request_id", hidden)
                verify = Mock(return_value=True)
                host._owner_review_dispatcher = SimpleNamespace(
                    _controller=SimpleNamespace(_verify=verify))
                with patch("sora_commander_next.jobs.subprocess.Popen",
                           side_effect=AssertionError("replay forbidden")) as spawn:
                    sdk = build_server(host)
                    _, recovered = asyncio.run(sdk.call_tool(
                        "recovery_overview", {}))
                    row = recovered["sessions"][0]
                    self.assertTrue(row["command_identified"])
                    self.assertEqual(row["request_id"], "a" * 32)
                    self.assertEqual(row["safe_command"]["argv"], ["--version"])
                    self.assertEqual(row["safe_command"]["cwd"], d)
                    self.assertIn("--version", row["command_line"])
                    self.assertFalse(row["auto_retry"])
                    index = host.previous_output()
                    self.assertTrue(index["sessions"][0]["command_identified"])
                    detail = host.previous_output(sid)
                    self.assertTrue(detail["command_identified"])
                    self.assertIn("--version", detail["command_line"])
                    self.assertEqual(detail["status"], "COMPLETED")
                    self.assertEqual(detail["exit_code"], 0)
                    self.assertEqual(reopened.list_sessions()["count"], 0)
                    spawn.assert_not_called()
                self.assertGreaterEqual(verify.call_count, 3)
                verify.side_effect = PermissionError("locked")
                self.assertEqual(host.previous_output(sid)["status"], "DENY")
                locked = host.recovery_overview()
                self.assertNotIn("request_id", locked["sessions"][0])
                self.assertNotIn("command_line", locked["sessions"][0])
            finally:
                reopened.close()

    def test_inline_script_or_secret_never_persisted_even_when_executed(self):
        sentinel = "FAKE-PRIVATE-VALUE-DO-NOT-STORE"
        source = "print('OK') # " + sentinel
        with tempfile.TemporaryDirectory(prefix="sora-unsafe-cmd-") as d:
            journal, archive, sid = self._one_job(d, ("-c", source), "b" * 32)
            raw = archive.read_text(encoding="ascii")
            self.assertNotIn(sentinel, raw)
            self.assertNotIn("safe_command", raw)
            reopened = JobManager(journal_path=journal, output_archive_path=archive)
            try:
                host = Host(jobs=reopened)
                host._owner_review_dispatcher = SimpleNamespace(
                    _controller=SimpleNamespace(_verify=Mock(return_value=True)))
                row = host.previous_output(sid)
                self.assertFalse(row["command_identified"])
                self.assertNotIn("command_line", row)
                self.assertNotIn("safe_command", row)
                recovered = host.recovery_overview()["sessions"][0]
                self.assertFalse(recovered["command_identified"])
                self.assertEqual(recovered["request_id"], "b" * 32)
            finally:
                reopened.close()

    def test_old_archive_and_forged_full_command_are_rejected_safely(self):
        from time import time
        with tempfile.TemporaryDirectory(prefix="sora-legacy-cmd-") as d:
            archive = Path(d) / "owner-output-archive.json"
            original = {
                "request_id": "c" * 32, "session_id": "d" * 48,
                "status": "COMPLETED", "exit_code": 0,
                "captured_bytes": 0, "output_incomplete": False,
                "archived_truncated": False, "saved_at": int(time()),
                "chunks": [],
            }
            write_result_checkpoint(archive, original)
            self.assertNotIn("safe_command",
                             read_previous_results(archive)["results"][0])
            stored = archive.read_bytes()
            import copy
            for forged in (
                {"executable": sys.executable,
                 "argv": ["-c", "FAKE_SECRET"], "cwd": d},
                {"executable": sys.executable,
                 "argv": ["--version"], "cwd": d, "extra": "bad"},
                {"executable": sys.executable,
                 "argv": ["--version"], "cwd": d + "/secret"},
            ):
                with self.subTest(forged=forged):
                    candidate = copy.deepcopy(original)
                    candidate["safe_command"] = forged
                    with self.assertRaises(ResultArchiveError):
                        write_result_checkpoint(archive, candidate)
                    self.assertEqual(archive.read_bytes(), stored)


if __name__ == "__main__":
    unittest.main()
