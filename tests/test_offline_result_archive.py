"""Inert archive persistence tests, no JobManager launch/Owner UI/real IPC.

Uses disposable temporary directories and separate Python processes only.
Stored output is synthetic. Never verifies a protected Owner directory ACL.
"""
import base64
from pathlib import Path
import copy
import json
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.offline_result_archive import (
    MAX_CAPTURE, MAX_AGE_SECONDS, MAX_RECORDS, ResultArchiveError,
    read_previous_results, write_result_checkpoint
)


def checkpoint(*, session="a"*48, request="mock-request", state="RUNNING",
               code=None, parts=(), saved=1000000, captured=None,
               output_incomplete=False):
    chunks = []
    offset = 0
    for stream, data in parts:
        chunks.append({"offset": offset, "stream": stream,
                       "data_base64": base64.b64encode(data).decode("ascii")})
        offset += len(data)
    return {
        "session_id": session, "request_id": request, "status": state,
        "exit_code": code, "captured_bytes": (
            offset if captured is None else captured),
        "output_incomplete": output_incomplete, "saved_at": saved,
        "archived_truncated": (
            captured is not None and captured > offset),
        "chunks": chunks,
    }


class OfflineResultArchiveTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix="next-data-result-")
        self.addCleanup(t.cleanup)
        self.path = Path(t.name) / "owner-output-archive.json"

    def test_complete_output_survives_read_from_separate_os_process(self):
        beginning = checkpoint(parts=[("stdout", b"early\x00bytes\n")])
        write_result_checkpoint(self.path, beginning, now=1000000)
        finished = checkpoint(
            state="COMPLETED", code=0,
            parts=[("stdout", b"early\x00bytes\n"),
                   ("stderr", b"warning \xff\n")],
            saved=1000001,
        )
        write_result_checkpoint(self.path, finished, now=1000001)
        child = (
            "import sys,json,base64;"
            "sys.path.insert(0,sys.argv[1]);"
            "from sora_commander_next.offline_result_archive import read_previous_results;"
            "o=read_previous_results(sys.argv[2],now=1000002);"
            "print(json.dumps(o))"
        )
        run = subprocess.run(
            [sys.executable, "-I", "-B", "-c", child,
             str(ROOT / "src"), str(self.path)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr[-500:])
        obj = json.loads(run.stdout)
        self.assertEqual(obj["count"], 1)
        row = obj["results"][0]
        self.assertEqual(row["status"], "COMPLETED")
        self.assertEqual(row["exit_code"], 0)
        self.assertEqual(
            b"".join(base64.b64decode(c["data_base64"])
                     for c in row["chunks"]),
            b"early\x00bytes\nwarning \xff\n")
        self.assertNotIn("executable", self.path.read_text())
        self.assertNotIn("argv", self.path.read_text())

    def test_unproven_completion_cannot_replace_durable_running_checkpoint(self):
        running = checkpoint(parts=[("stdout", b"work may have stopped\n")])
        write_result_checkpoint(self.path, running, now=1000000)
        durable = self.path.read_bytes()
        for code in (None, 1, -1):
            with self.subTest(code=code), self.assertRaises(ResultArchiveError):
                write_result_checkpoint(self.path, checkpoint(
                    state="COMPLETED", code=code, saved=1000001,
                    parts=[("stdout", b"work may have stopped\n")]),
                    now=1000001)
            self.assertEqual(self.path.read_bytes(), durable)
        row = read_previous_results(self.path, now=1000001)["results"][0]
        self.assertEqual(row["status"], "INTERRUPTED_UNKNOWN")
        self.assertIsNone(row["exit_code"])
        self.assertTrue(row["output_incomplete"])

    def test_unproven_completion_also_rejected_on_archive_read(self):
        for code in (None, 1, -1):
            with self.subTest(code=code):
                raw = json.dumps({"version": 1, "results": [checkpoint(
                    state="COMPLETED", code=code)]}).encode("ascii")
                self.path.write_bytes(raw)
                with self.assertRaises(ResultArchiveError):
                    read_previous_results(self.path, now=1000001)

    def test_future_dated_completion_is_not_trusted_on_recovery(self):
        for saved, allowed in ((1000060, True), (1000061, False),
                               (1000200, False)):
            with self.subTest(saved=saved):
                raw = json.dumps({"version": 1, "results": [checkpoint(
                    state="COMPLETED", code=0, saved=saved)]}).encode("ascii")
                self.path.write_bytes(raw)
                if allowed:
                    recovered = read_previous_results(self.path, now=1000000)
                    self.assertEqual(recovered["results"][0]["status"],
                                     "COMPLETED")
                else:
                    with self.assertRaises(ResultArchiveError):
                        read_previous_results(self.path, now=1000000)

    def test_interrupted_checkpoint_never_promotes_running_to_success(self):
        write_result_checkpoint(self.path, checkpoint(
            parts=[("stdout", b"checkpoint before power loss\n")]),
            now=1000000)
        old = read_previous_results(self.path, now=1000001)
        self.assertEqual(old["results"][0]["status"], "INTERRUPTED_UNKNOWN")
        self.assertIsNone(old["results"][0]["exit_code"])
        self.assertTrue(old["results"][0]["output_incomplete"])
        self.assertEqual(old["count"], 1)
        self.assertEqual(read_previous_results(self.path, now=1000001), old)

    def test_corrupt_unrecognized_and_duplicate_json_are_denied(self):
        invalid = (
            b"invalid", b"null", b'{"version":1,"results":null}',
            b'{"version":1,"version":1,"results":[]}',
            b'{"version":2,"results":[]}',
            b'{"version":1,"results":[],"grant":true}',
            b"x" * (512 * 1024 + 1),
        )
        for raw in invalid:
            with self.subTest(raw=raw[:50]):
                self.path.write_bytes(raw)
                with self.assertRaises(ResultArchiveError):
                    read_previous_results(self.path, now=1000001)

    def test_over_cap_or_forged_fields_never_written(self):
        too_long = checkpoint(parts=[("stdout", b"x"*(MAX_CAPTURE+1))])
        bad_claim = checkpoint(parts=[("stdout", b"good")])
        bad_claim["grant"] = "APPROVED"
        modified = checkpoint(parts=[("stderr", b"hi")])
        modified["chunks"][0]["offset"] = 2
        for record in (too_long, bad_claim, modified):
            with self.subTest(record=record["session_id"]):
                with self.assertRaises(ResultArchiveError):
                    write_result_checkpoint(self.path, record, now=1000000)
                self.assertFalse(self.path.exists())

    def test_truncated_checkpoint_remembers_loss(self):
        entry = checkpoint(
            parts=[("stdout", b"first")], captured=7000,
            output_incomplete=True)
        write_result_checkpoint(self.path, entry, now=1000000)
        previous = read_previous_results(self.path, now=1000000)
        self.assertTrue(previous["results"][0]["archived_truncated"])
        self.assertEqual(previous["results"][0]["captured_bytes"], 7000)

    def test_stale_writes_and_session_reuse_conflict(self):
        first = checkpoint(parts=[("stdout", b"hello")])
        write_result_checkpoint(self.path, first, now=1000000)
        with self.assertRaises(ResultArchiveError):
            write_result_checkpoint(self.path, checkpoint(
                request="someone-else", parts=[("stdout", b"hello")]),
                now=1000000)
        with self.assertRaises(ResultArchiveError):
            write_result_checkpoint(self.path, checkpoint(
                saved=999999, parts=[("stdout", b"hello")]), now=1000000)
        completed = checkpoint(
            state="COMPLETED", code=0, saved=1000001,
            parts=[("stdout", b"hello")])
        write_result_checkpoint(self.path, completed, now=1000001)
        with self.assertRaises(ResultArchiveError):
            write_result_checkpoint(self.path, checkpoint(
                state="RUNNING", saved=1000002,
                parts=[("stdout", b"hello")]), now=1000002)
        self.assertEqual(read_previous_results(
            self.path, now=1000002)["results"][0]["status"], "COMPLETED")

    def test_expiry_limit_and_replacement_are_bounded(self):
        for index in range(MAX_RECORDS + 3):
            item = checkpoint(
                session=f"{index:048x}", request=f"req-{index}",
                state="COMPLETED", code=0, saved=1000000+index)
            write_result_checkpoint(
                self.path, item, now=1000000+index)
        entries = read_previous_results(
            self.path, now=1000000 + MAX_RECORDS+2)
        self.assertEqual(entries["count"], MAX_RECORDS)
        self.assertNotIn(f"{0:048x}",
                         [x["session_id"] for x in entries["results"]])
        self.assertEqual(
            read_previous_results(
                self.path, now=1000000+MAX_AGE_SECONDS+MAX_RECORDS+5)["count"],
            0)

    def test_requires_fixed_filename_and_existing_directory(self):
        t = self.path.parent
        bad_paths = [t/"arbitrary.json",
                     t/"missing"/"owner-output-archive.json",
                     Path("owner-output-archive.json")]
        for path in bad_paths:
            with self.subTest(path=path):
                with self.assertRaises(ResultArchiveError):
                    write_result_checkpoint(path, checkpoint(), now=1000000)
        self.assertFalse(self.path.exists())

    def test_reading_missing_archive_is_not_fabrication(self):
        self.assertEqual(
            read_previous_results(self.path, now=1000000),
            {"results": [], "count": 0})


if __name__ == "__main__":
    unittest.main()
