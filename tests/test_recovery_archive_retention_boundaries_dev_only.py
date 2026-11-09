"""DEV_ONLY capacity/age boundary checks for read-only manual recovery.

Only synthetic, disposable journal/archive files are touched; the live Host,
Owner's real output archive, credentials, and tunnel are never accessed.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import base64
import sys
import tempfile
import time
import unittest

from sora_commander_next.host import Host
from sora_commander_next.jobs import JobManager
from sora_commander_next.offline_result_archive import (
    MAX_AGE_SECONDS, MAX_ARCHIVE_SIZE, MAX_RECORDS,
    read_previous_results, write_result_checkpoint,
)
from sora_commander_next.session_journal import write_snapshot


def _entry(sid, rid, saved_at, *, safe_command=None, output=b""):
    chunks = ([{"offset": 0, "stream": "stdout",
                "data_base64": base64.b64encode(output).decode("ascii")}]
              if output else [])
    return {
        "request_id": rid, "session_id": sid, "status": "COMPLETED",
        "exit_code": 0, "captured_bytes": len(output),
        "output_incomplete": False, "archived_truncated": False,
        "saved_at": saved_at, "chunks": chunks,
        **({"safe_command": safe_command} if safe_command is not None else {}),
    }


def _journal_entry(sid, *, status="COMPLETED", exit_code=0):
    return {
        "session_id": sid, "status": status, "pid": 4242,
        "exit_code": exit_code, "captured_bytes": 0,
        "output_incomplete": False,
    }


def _host(journal, archive):
    manager = JobManager(journal_path=journal, output_archive_path=archive)
    host = Host(jobs=manager)
    reviewer = Mock(return_value=True)
    host._owner_review_dispatcher = SimpleNamespace(
        _controller=SimpleNamespace(_verify=reviewer))
    return manager, host, reviewer


class ArchiveRetentionRecoveryTests(unittest.TestCase):
    def test_evicted_session_stays_journal_only_no_guess_or_replay(self):
        # Deliberately fill the bounded archive with 33 FAKE safe records,
        # and prove the evicted oldest cannot be identified from archive.
        with tempfile.TemporaryDirectory(prefix="sora-archive-boundary-") as tmp:
            home = Path(tmp)
            journal = home / "journal.json"
            archive = home / "owner-output-archive.json"
            now = int(time.time())
            evicted_sid = f"{0:048x}"
            write_snapshot(journal, [_journal_entry(evicted_sid)])
            safe = {"executable": sys.executable,
                    "argv": ["--version"], "cwd": str(home)}
            for i in range(MAX_RECORDS + 1):
                write_result_checkpoint(
                    archive,
                    _entry(f"{i:048x}", f"synthetic-{i:03d}",
                           now - (MAX_RECORDS - i), safe_command=safe),
                    now=now,
                )
            self.assertLessEqual(archive.stat().st_size, MAX_ARCHIVE_SIZE)
            self.assertEqual(read_previous_results(archive, now=now)["count"],
                             MAX_RECORDS)
            manager, host, verify = _host(journal, archive)
            try:
                with patch("sora_commander_next.jobs.subprocess.Popen",
                           side_effect=AssertionError("automatic replay forbidden")) as spawn:
                    result = host.recovery_overview()
                    self.assertEqual(result["mode"], "READ_ONLY_MANUAL_RECOVERY")
                    self.assertFalse(result["auto_retry"])
                    self.assertEqual(result["count"], MAX_RECORDS + 1)
                    journal_only = next(
                        r for r in result["sessions"]
                        if r["session_id"] == evicted_sid)
                    self.assertEqual(journal_only["origin"], "PREVIOUS_PROCESS")
                    self.assertFalse(journal_only["command_identified"])
                    self.assertNotIn("request_id", journal_only)
                    self.assertNotIn("safe_command", journal_only)
                    self.assertNotIn("command_line", journal_only)
                    self.assertEqual(host.previous_output(evicted_sid)["status"],
                                     "UNKNOWN")
                    for row in result["sessions"]:
                        self.assertFalse(row["auto_retry"])
                        if row["origin"] == "ARCHIVE_ONLY":
                            self.assertEqual(row["status"], "OUTCOME_UNKNOWN")
                            self.assertEqual(row["next_step"],
                                             "INSPECT_EFFECTS_MANUALLY")
                    self.assertEqual(host.list_sessions()["count"], 0)
                    spawn.assert_not_called()
                self.assertGreaterEqual(verify.call_count, 2)
            finally:
                manager.close()

    def test_seven_day_expiry_removes_correlation_not_journal_warning(self):
        with tempfile.TemporaryDirectory(prefix="sora-archive-expiry-") as tmp:
            home = Path(tmp)
            journal = home / "journal.json"
            archive = home / "owner-output-archive.json"
            sid = "e" * 48
            now = int(time.time())
            write_snapshot(journal, [_journal_entry(
                sid, status="RUNNING", exit_code=None)])
            write_result_checkpoint(
                archive,
                _entry(sid, "synthetic-expiry", now - MAX_AGE_SECONDS,
                       safe_command={"executable": sys.executable,
                                     "argv": ["--version"], "cwd": str(home)}),
                now=now,
            )
            self.assertEqual(read_previous_results(archive, now=now)["count"], 1)
            self.assertEqual(read_previous_results(archive, now=now+1)["count"], 0)
            # Expiry is enforced when a NEW Host loads archived data; an
            # already-running Host keeps its immutable loaded snapshot.
            with patch("sora_commander_next.offline_result_archive.time.time",
                       return_value=now+1):
                manager, host, verify = _host(journal, archive)
            try:
                with patch("sora_commander_next.jobs.subprocess.Popen",
                           side_effect=AssertionError("automatic replay forbidden")) as spawn:
                    result = host.recovery_overview()
                    self.assertEqual(result["count"], 1)
                    row = result["sessions"][0]
                    self.assertEqual(row["status"], "INTERRUPTED_UNKNOWN")
                    self.assertEqual(row["next_step"], "INSPECT_EFFECTS_MANUALLY")
                    self.assertFalse(row["command_identified"])
                    self.assertNotIn("request_id", row)
                    self.assertNotIn("command_line", row)
                    self.assertFalse(result["auto_retry"])
                    self.assertEqual(host.previous_output(sid)["status"], "UNKNOWN")
                    self.assertEqual(host.list_sessions()["count"], 0)
                    spawn.assert_not_called()
                self.assertGreaterEqual(verify.call_count, 2)
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
