"""Previously archived stdout/stderr bytes must be immutable across checkpoints.

All tests use a disposable archive in the canonical repo's existing temp/.
No real job, Owner review, Host restart or private archive is touched.
"""
import base64
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sora_commander_next.offline_result_archive import (
    ResultArchiveError, read_previous_results, write_result_checkpoint,
)

TEMP = Path(__file__).resolve().parents[1] / "temp"


def checkpoint(parts=(), *, state="RUNNING", code=None, saved=1000000,
               captured=None, incomplete=False):
    chunks, offset = [], 0
    for stream, raw in parts:
        assert stream in ("stdout", "stderr")
        assert isinstance(raw, bytes) and raw
        chunks.append({"offset": offset, "stream": stream,
                       "data_base64": base64.b64encode(raw).decode("ascii")})
        offset += len(raw)
    captured = offset if captured is None else captured
    return {
        "request_id": "synthetic-request", "session_id": "a" * 48,
        "status": state, "exit_code": code,
        "captured_bytes": captured, "output_incomplete": incomplete,
        "saved_at": saved, "archived_truncated": captured > offset,
        "chunks": chunks,
    }


class ImmutableArchivedOutputTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(TEMP.is_dir() and not TEMP.is_symlink())
        holder = TemporaryDirectory(prefix="scn-result-prefix-", dir=TEMP)
        self.addCleanup(holder.cleanup)
        self.path = Path(holder.name) / "owner-output-archive.json"

    def put(self, entry, now=None):
        write_result_checkpoint(
            self.path, entry, now=(entry["saved_at"] if now is None else now)
        )

    def assert_unchanged_on_rejected(self, proposed):
        before = self.path.read_bytes()
        with self.assertRaises(ResultArchiveError):
            self.put(proposed)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            list(self.path.parent.glob(".sora-result-*.tmp")), []
        )

    def test_previous_stdout_bytes_cannot_be_rewritten(self):
        self.put(checkpoint([("stdout", b"first checkpoint\n")]))
        self.assert_unchanged_on_rejected(checkpoint(
            [("stdout", b"FAKE_PRIVATE_CHANGE\n")], saved=1000001,
        ))

    def test_previous_stream_label_cannot_change(self):
        self.put(checkpoint([("stdout", b"first")]))
        self.assert_unchanged_on_rejected(checkpoint(
            [("stderr", b"first")], saved=1000001,
        ))

    def test_archive_cannot_shrink_its_prior_prefix_with_same_capture_count(self):
        self.put(checkpoint([("stdout", b"abcdef")]))
        self.assert_unchanged_on_rejected(checkpoint(
            [("stdout", b"abc")], saved=1000001, captured=6,
            incomplete=True,
        ))

    def test_terminal_recheckpoint_cannot_replace_output(self):
        initial = checkpoint(
            [("stdout", b"completed")], state="COMPLETED",
            code=0, saved=1000000,
        )
        self.put(initial)
        self.assert_unchanged_on_rejected(checkpoint(
            [("stdout", b"FAKE_OTHER")], state="COMPLETED",
            code=0, saved=1000001,
        ))

    def test_append_and_chunk_coalescing_are_still_allowed(self):
        self.put(checkpoint([("stdout", b"ab"), ("stdout", b"cd")]))
        self.put(checkpoint(
            [("stdout", b"abcd"), ("stderr", b"\nwarning")],
            state="COMPLETED", code=0, saved=1000001,
        ))
        rows = read_previous_results(self.path, now=1000001)["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "COMPLETED")
        self.assertEqual(rows[0]["exit_code"], 0)
        self.assertEqual(rows[0]["captured_bytes"], len(b"abcd\nwarning"))

    def test_same_prefix_with_higher_capture_count_and_truncation_is_allowed(self):
        # A bounded 8KiB archived prefix need not grow with total capture.
        self.put(checkpoint([("stdout", b"prefix")]))
        self.put(checkpoint(
            [("stdout", b"prefix")], saved=1000001,
            captured=80, incomplete=True,
        ))
        row = read_previous_results(self.path, now=1000001)["results"][0]
        self.assertTrue(row["archived_truncated"])
        self.assertEqual(row["captured_bytes"], 80)


if __name__ == "__main__":
    unittest.main()
