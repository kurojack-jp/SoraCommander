"""Single opt-in disposable job output recovery after NEW JobManager instance.

This is not the previously safety-blocked concurrent journal lock-order test
and not a production Owner Broker, named-pipe, or full-suite regression.
"""
import asyncio
import base64
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.commands import Command
from sora_commander_next.jobs import JobManager
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import _validate_arguments, build_server
from sora_commander_next.offline_result_archive import read_previous_results


def raw_output(row):
    return b"".join(base64.b64decode(c["data_base64"])
                    for c in row["chunks"])


class ArchiveJobIntegrationTests(unittest.TestCase):
    def test_cross_manager_archived_output_pages_avoid_relaunch(self):
        # Real disposable Windows child, then a completely NEW JobManager
        # instance reads archived 4KiB slices without possessing live jobs.
        import secrets
        with tempfile.TemporaryDirectory(prefix="next-restart-pages-") as d:
            home = Path(d)
            journal = home / "journal.json"
            archive = home / "owner-output-archive.json"
            old = JobManager(journal_path=journal, output_archive_path=archive)
            req = Command(
                sys.executable, ("-I", "-S", "-c",
                 "import sys;sys.stdout.buffer.write(b'A'*4500);"
                 "sys.stdout.flush();sys.stderr.buffer.write(b'B'*700);"
                 "sys.stderr.flush()"),
                d, timeout=5, output_limit=8192,
                request_id=secrets.token_hex(16))
            try:
                start = old.start(req)
                sid = start["session_id"]
                self.assertEqual(old.wait(sid)["status"], "COMPLETED")
                with old._lock:
                    thread = old._jobs[sid].thread
                thread.join(timeout=4)
                self.assertFalse(thread.is_alive())
            finally:
                old.close()
            restored = JobManager(journal_path=journal, output_archive_path=archive)
            try:
                self.assertEqual(restored.list_sessions()["count"], 0)
                first = restored.previous_output_page(req.request_id, sid, 0)
                self.assertEqual((first["status"], first["exit_code"]),
                                 ("COMPLETED", 0))
                self.assertEqual((first["offset"], first["next_offset"],
                                  first["archived_bytes"]), (0, 4096, 5200))
                self.assertEqual(base64.b64decode(first["stdout_base64"]), b"A"*4096)
                self.assertFalse(first["end_of_archived"])
                self.assertFalse(first["archived_truncated"])
                second = restored.previous_output_page(
                    req.request_id, sid, first["next_offset"])
                self.assertEqual((second["offset"], second["next_offset"]),
                                 (4096, 5200))
                self.assertEqual(base64.b64decode(second["stdout_base64"]), b"A"*404)
                self.assertEqual(base64.b64decode(second["stderr_base64"]), b"B"*700)
                self.assertTrue(second["end_of_archived"])
                final = restored.previous_output_page(req.request_id, sid, 5200)
                self.assertEqual(final["next_offset"], 5200)
                self.assertEqual(base64.b64decode(final["stdout_base64"]), b"")
                self.assertEqual(restored.list_sessions()["count"], 0)
                self.assertEqual(restored.previous_output_page(
                    "f"*32, sid)["status"], "UNKNOWN")
                self.assertEqual(restored.previous_output_page(
                    req.request_id, "f"*48)["status"], "UNKNOWN")
                self.assertEqual(restored.previous_output_page(
                    req.request_id, sid, 5201)["error"],
                    "OffsetPastArchivedOutput")
            finally:
                restored.close()

            # A NEW Windows Python process can retrieve the SAME stored
            # pages without taking ownership of, or re-running, the old job.
            code = (
                "import sys,json;"
                "sys.path.insert(0,sys.argv[1]);"
                "from sora_commander_next.jobs import JobManager;"
                "m=JobManager(journal_path=sys.argv[2],"
                "output_archive_path=sys.argv[3]);"
                "a=m.previous_output_page(sys.argv[4],sys.argv[5]);"
                "b=m.previous_output_page(sys.argv[4],sys.argv[5],"
                "a['next_offset']);"
                "print(json.dumps({'a':a,'b':b,'live':m.list_sessions()['count']}));"
                "m.close()"
            )
            proc = subprocess.run(
                [sys.executable, "-I", "-S", "-c", code,
                 str(ROOT / "src"), str(journal), str(archive),
                 req.request_id, sid],
                text=True, capture_output=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            detached = json.loads(proc.stdout)
            self.assertEqual(detached["live"], 0)
            self.assertEqual(detached["a"]["next_offset"], 4096)
            self.assertEqual(detached["b"]["next_offset"], 5200)
            self.assertEqual(detached["b"]["exit_code"], 0)
            self.assertEqual(
                base64.b64decode(detached["b"]["stderr_base64"]), b"B"*700)

    def test_recovered_interrupted_and_truncated_output_not_promoted(self):
        from sora_commander_next.offline_result_archive import write_result_checkpoint
        with tempfile.TemporaryDirectory(prefix="next-partial-restart-pages-") as d:
            home = Path(d)
            archive = home / "owner-output-archive.json"
            request_id, session_id = "a"*32, "b"*48
            item = {
                "request_id": request_id, "session_id": session_id,
                "status": "RUNNING", "exit_code": None, "captured_bytes": 8000,
                "output_incomplete": False, "saved_at": int(time.time()),
                "archived_truncated": True,
                "chunks": [{"offset": 0, "stream": "stderr",
                            "data_base64": base64.b64encode(
                                b"E"*5000).decode("ascii")}],
            }
            write_result_checkpoint(archive, item)
            fresh = JobManager(journal_path=home/"journal.json",
                               output_archive_path=archive)
            try:
                first = fresh.previous_output_page(request_id, session_id)
                self.assertEqual(first["status"], "INTERRUPTED_UNKNOWN")
                self.assertIsNone(first["exit_code"])
                self.assertTrue(first["output_incomplete"])
                self.assertTrue(first["archived_truncated"])
                self.assertEqual((first["captured_bytes"], first["archived_bytes"]),
                                 (8000, 5000))
                self.assertEqual(first["next_offset"], 4096)
                later = fresh.previous_output_page(request_id, session_id, 4096)
                self.assertEqual(base64.b64decode(later["stderr_base64"]),
                                 b"E"*904)
                self.assertEqual(later["next_offset"], 5000)
                self.assertTrue(later["end_of_archived"])
                self.assertEqual(fresh.previous_output_page(
                    request_id, session_id, 5001)["error"],
                    "OffsetPastArchivedOutput")
                self.assertEqual(fresh.list_sessions()["count"], 0)
            finally:
                fresh.close()

    def test_archived_page_requires_optin_and_strict_ids(self):
        with tempfile.TemporaryDirectory(prefix="next-no-page-") as d:
            without = JobManager(journal_path=Path(d)/"journal.json")
            try:
                self.assertEqual(without.previous_output_page(
                    "a"*32, "b"*48)["error"], "ArchiveNotEnabled")
            finally:
                without.close()
            with_archive = JobManager(
                journal_path=Path(d)/"journal.json",
                output_archive_path=Path(d)/"owner-output-archive.json")
            try:
                for req, sid, offset in (
                    ("bad", "b"*48, 0),
                    ("a"*32, "bad", 0),
                    ("a"*32, "b"*48, True),
                    ("a"*32, "b"*48, 8193),
                    ("a"*32, "b"*48, -1),
                ):
                    with self.subTest(req=req, sid=sid, offset=offset):
                        self.assertEqual(with_archive.previous_output_page(
                            req, sid, offset)["error"], "InvalidArchivedPage")
                self.assertEqual(with_archive.previous_output_page(
                    "a"*32, "b"*48)["status"], "UNKNOWN")
                self.assertEqual(with_archive.list_sessions()["count"], 0)
            finally:
                with_archive.close()

    def test_complete_job_recovers_binary_stdout_and_stderr_without_replay(self):
        with tempfile.TemporaryDirectory(prefix="next-archived-job-") as d:
            home = Path(d)
            journal = home / "journal.json"
            archive = home / "owner-output-archive.json"
            manager = JobManager(
                journal_path=journal, output_archive_path=archive)
            request = Command(
                sys.executable,
                ("-I", "-S", "-c",
                 "import sys;sys.stdout.buffer.write(b'OK\\x00OUT\\n');"
                 "sys.stdout.flush();"
                 "sys.stderr.buffer.write(b'ERROR\\xff\\n');"
                 "sys.stderr.flush()"),
                str(home), timeout=5, output_limit=8192,
                request_id="archive-job-test",
            )
            try:
                result = manager.start(request)
                self.assertEqual(result["status"], "RUNNING", result)
                finished = manager.wait(result["session_id"])
                self.assertEqual(finished["status"], "COMPLETED", finished)
                # The supervisor can set done just before the journal/archive
                # writers finish; wait for its actual thread completion.
                with manager._lock:
                    thread = manager._jobs[result["session_id"]].thread
                thread.join(timeout=4)
                self.assertFalse(thread.is_alive())
                self.assertTrue(archive.is_file())
            finally:
                manager.close()
            reopened = JobManager(
                journal_path=journal, output_archive_path=archive)
            try:
                self.assertEqual(reopened.list_sessions()["count"], 0)
                self.assertEqual(reopened.previous_sessions()["count"], 1)
                self.assertEqual(reopened.previous_sessions()[
                    "sessions"][0]["status"], "COMPLETED")
                old = reopened.previous_output(result["session_id"])
                self.assertEqual(old["status"], "COMPLETED")
                self.assertEqual(old["exit_code"], 0)
                self.assertEqual(old["request_id"], "archive-job-test")
                self.assertEqual(
                    b"".join(base64.b64decode(c["data_base64"])
                             for c in old["chunks"]
                             if c["stream"] == "stdout"),
                    b"OK\x00OUT\n")
                self.assertEqual(
                    b"".join(base64.b64decode(c["data_base64"])
                             for c in old["chunks"]
                             if c["stream"] == "stderr"),
                    b"ERROR\xff\n")
                self.assertNotIn("argv", archive.read_text(encoding="utf-8"))
                self.assertNotIn("exe", archive.read_text(encoding="utf-8"))
                self.assertEqual(reopened.list_sessions()["count"], 0)
                host = Host(jobs=reopened)
                # Mocked Owner-session check only. Real Windows Owner SID
                # and locked-desktop acceptance remain unverified.
                from types import SimpleNamespace
                from unittest.mock import Mock
                owner_verify = Mock(return_value=True)
                host._owner_review_dispatcher = SimpleNamespace(
                    _controller=SimpleNamespace(_verify=owner_verify))
                index = host.previous_output()
                self.assertEqual(index["count"], 1)
                self.assertEqual(index["sessions"][0]["session_id"],
                                 result["session_id"])
                self.assertNotIn("chunks", index["sessions"][0])
                recovered = host.previous_output(result["session_id"])
                self.assertEqual(recovered["status"], "COMPLETED")
                self.assertEqual(recovered["stdout"], "OK\x00OUT\n")
                self.assertEqual(recovered["stderr"], "ERROR\ufffd\n")
                # Real MCP SDK surface, but no daemon, tunnel or actual
                # Owner-approved remote command. Retrieval must not spawn.
                sdk = build_server(host)
                sdk_result = asyncio.run(sdk.call_tool(
                    "previous_output", {"session_id": result["session_id"]}))
                self.assertIn("COMPLETED", str(sdk_result))
                self.assertIn("OK", str(sdk_result))
                self.assertEqual(reopened.list_sessions()["count"], 0)
            finally:
                reopened.close()

    def test_owner_locked_or_missing_reviewer_cannot_read_saved_output(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory(prefix="next-archive-deny-") as d:
            root = Path(d)
            jobs = JobManager(
                journal_path=root / "journal.json",
                output_archive_path=root / "owner-output-archive.json")
            try:
                host = Host(jobs=jobs)
                # An enabled archive without the local Owner controller is
                # denied, regardless of knowledge of the session ID.
                self.assertEqual(host.previous_output()["status"], "DENY")
                self.assertEqual(host.previous_output("a" * 48)["status"], "DENY")
                verifier = Mock(side_effect=PermissionError("screen locked"))
                host._owner_review_dispatcher = SimpleNamespace(
                    _controller=SimpleNamespace(_verify=verifier))
                for query in (None, "a" * 48):
                    with self.subTest(query=query):
                        result = host.previous_output(query)
                        self.assertEqual(result, {
                            "status": "DENY",
                            "error": "OwnerAuthorizationRequired"})
                self.assertEqual(verifier.call_count, 2)
                verifier.side_effect = OSError("session unknown")
                self.assertEqual(host.previous_output()["status"], "ERROR")
            finally:
                jobs.close()

    def test_running_stdout_has_a_durable_partial_checkpoint(self):
        # This is a disposable local child, never a live Owner/MCP command.
        with tempfile.TemporaryDirectory(prefix="next-partial-log-") as d:
            home = Path(d)
            archive = home / "owner-output-archive.json"
            jobs = JobManager(journal_path=home / "journal.json",
                              output_archive_path=archive)
            try:
                request = Command(
                    sys.executable,
                    ("-I", "-S", "-u", "-c",
                     "import sys,time;"
                     "sys.stdout.write('EARLY-CHECKPOINT\\n');"
                     "sys.stdout.flush();"
                     "time.sleep(2)"),
                    d, timeout=5, request_id="partial-output-test")
                sid = jobs.start(request)["session_id"]
                seen = None
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    rows = read_previous_results(archive)["results"]
                    row = next((v for v in rows if v["session_id"] == sid), None)
                    if row and b"EARLY-CHECKPOINT" in raw_output(row):
                        seen = row
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(seen, "partial output checkpoint absent")
                self.assertEqual(seen["status"], "INTERRUPTED_UNKNOWN")
                self.assertTrue(seen["output_incomplete"])
                # The job has not been resurrected; we inspect a checkpoint
                # while the SAME disposable job runs normally.
                self.assertIn("EARLY-CHECKPOINT", jobs.read(sid)["stdout"])
                finished = jobs.wait(sid)
                self.assertEqual(finished["status"], "COMPLETED")
            finally:
                jobs.close()

    def test_previous_output_wire_is_read_only_and_strict(self):
        _validate_arguments("previous_output", {})
        _validate_arguments("previous_output", {"session_id": "a" * 48})
        for candidate in (None, True, 3, "", "B" * 48, "z" * 48,
                          "a" * 47, "a" * 49):
            with self.subTest(value=candidate):
                with self.assertRaises(ValueError):
                    _validate_arguments(
                        "previous_output", {"session_id": candidate})
        with self.assertRaises(ValueError):
            _validate_arguments(
                "previous_output", {"session_id": "a" * 48,
                                    "approve": True})

    def test_archive_requires_separately_opted_in_colocated_journal(self):
        with tempfile.TemporaryDirectory(prefix="next-archive-off-") as d:
            root = Path(d)
            archive = root / "owner-output-archive.json"
            with self.assertRaises(ValueError):
                JobManager(output_archive_path=archive)
            with tempfile.TemporaryDirectory() as outside:
                with self.assertRaises(ValueError):
                    JobManager(
                        journal_path=root / "journal.json",
                        output_archive_path=Path(outside)
                        / "owner-output-archive.json")
            normal = JobManager(journal_path=root / "journal.json")
            try:
                self.assertEqual(normal.previous_output("a" * 48), {
                    "status": "DENY", "error": "ArchiveNotEnabled"})
                self.assertFalse(archive.exists())
            finally:
                normal.close()


if __name__ == "__main__":
    unittest.main()
