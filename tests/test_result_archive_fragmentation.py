"""Regression: small fragmented job output must not block final checkpoint.

Uses synthetic in-memory chunks, a disposable temp archive, and zero job
starts. Does not perform real Windows Owner authentication or probe protected
storage. This is not the separately blocked journal lock-order test.
"""
import base64
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.commands import Command
from sora_commander_next.jobs import JobManager
from sora_commander_next.offline_result_archive import read_previous_results


class FragmentedOutputArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="next-fragment-output-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.archive = self.root / "owner-output-archive.json"
        self.manager = JobManager(
            journal_path=self.root / "journal.json",
            output_archive_path=self.archive)
        self.addCleanup(self.manager.close)
        self.request = Command(
            sys.executable, ("--version",), str(self.root),
            request_id="fragment-test")
        self.job = SimpleNamespace(
            session_id="e" * 48,
            request=self.request,
            lock=RLock(),
            chunks=[],
            captured=0,
            status="RUNNING",
            exit_code=None,
            output_incomplete=False,
            overflow=False)

    def append_chunks(self, parts):
        for stream, value in parts:
            self.job.chunks.append((self.job.captured, stream, value))
            self.job.captured += len(value)

    def final(self):
        self.job.status = "COMPLETED"
        self.job.exit_code = 0
        self.manager._snapshot_result_archive(self.job)
        fresh = JobManager(
            journal_path=self.root / "journal.json",
            output_archive_path=self.archive)
        try:
            self.assertEqual(fresh.list_sessions()["count"], 0)
            self.assertEqual(fresh.previous_output()["count"], 1)
            return fresh.previous_output(self.job.session_id)
        finally:
            fresh.close()

    @staticmethod
    def chunk_bytes(row):
        return [
            (piece["stream"], base64.b64decode(piece["data_base64"]))
            for piece in row["chunks"]
        ]

    def test_hundreds_of_tiny_stdout_chunks_coalesce_and_keep_exit_code(self):
        self.append_chunks([("stdout", b"a") for _ in range(250)])
        self.manager._snapshot_result_archive(self.job)
        recorded = self.final()
        self.assertEqual(recorded["status"], "COMPLETED")
        self.assertEqual(recorded["exit_code"], 0)
        self.assertEqual(recorded["captured_bytes"], 250)
        self.assertFalse(recorded["archived_truncated"])
        self.assertEqual(self.chunk_bytes(recorded), [("stdout", b"a" * 250)])

    def test_alternating_streams_exceed_chunk_capacity_but_keep_final_state(self):
        self.append_chunks(
            [("stdout" if i % 2 == 0 else "stderr", b"x")
             for i in range(180)])
        self.manager._snapshot_result_archive(self.job)
        saved = self.final()
        self.assertEqual(saved["status"], "COMPLETED")
        self.assertEqual(saved["exit_code"], 0)
        self.assertEqual(saved["captured_bytes"], 180)
        self.assertEqual(len(saved["chunks"]), 128)
        self.assertTrue(saved["archived_truncated"])
        self.assertEqual(sum(len(value) for _, value in self.chunk_bytes(saved)), 128)
        self.assertEqual(
            [stream for stream, _ in self.chunk_bytes(saved)],
            ["stdout" if i % 2 == 0 else "stderr" for i in range(128)])

    def test_interrupted_fragmented_output_keeps_prefix_and_unknown_result(self):
        self.append_chunks(
            [("stdout" if i % 2 == 0 else "stderr", b"x")
             for i in range(150)])
        self.manager._snapshot_result_archive(self.job)
        recovered = read_previous_results(self.archive)["results"][0]
        self.assertEqual(recovered["status"], "INTERRUPTED_UNKNOWN")
        self.assertIsNone(recovered["exit_code"])
        self.assertTrue(recovered["output_incomplete"])
        self.assertTrue(recovered["archived_truncated"])
        self.assertEqual(len(recovered["chunks"]), 128)
        self.assertEqual(self.manager.list_sessions()["count"], 0)


if __name__ == "__main__":
    unittest.main()
