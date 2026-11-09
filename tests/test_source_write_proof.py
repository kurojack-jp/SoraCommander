"""D06 development review proof and exclusive-create faults; all writes mocked."""
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
import stat
import unittest
from unittest.mock import Mock, MagicMock, patch
from pathlib import Path

from test_sol_broker_vertical import GuardedTests, RID, TARGET, PARENTS
from sora_commander_next import source_review as subject


class SourceProofTests(GuardedTests):
    def test_terminal_create_retains_typed_nonce_bound_proof_once(self):
        self.submit()
        result = self.workflow.review_local(RID)
        proof = self.workflow._records[RID]["write_proof"]
        self.assertIs(type(proof), subject.SourceWriteProof)
        frame = subject.decode_frame(proof.review_bytes)
        self.assertEqual(result["nonce"], frame["nonce"])
        self.assertEqual(proof.after.data, b"x = 1\r\n")
        self.assertEqual(proof.authority, "NONE_DEV_DIAGNOSTIC")
        with self.assertRaises(FrozenInstanceError):
            proof.completed_tick = 1
        self.workflow.review_local(RID)
        self.assertEqual(self.store.writes, 1)
        self.assertIs(self.workflow._records[RID]["write_proof"], proof)

    def test_patch_proof_binds_before_and_after_content(self):
        self.store.seed(TARGET, b"x = 1\r\n")
        self.workflow.submit("request_source_patch", self.patch_args())
        self.workflow.review_local(RID)
        proof = self.workflow._records[RID]["write_proof"]
        self.assertEqual(proof.before.data, b"x = 1\r\n")
        self.assertEqual(proof.after.data, b"x = 2\r\n")
        wrong = subject.SourceSnapshot(b"bad", (1, 300, 3, 1, 1), PARENTS)
        with self.assertRaises(ValueError):
            replace(proof, before=wrong)
        with self.assertRaises(ValueError):
            replace(proof, after=wrong)

    def test_expired_proof_or_changed_parent_identity_rejected(self):
        self.submit()
        self.workflow.review_local(RID)
        proof = self.workflow._records[RID]["write_proof"]
        frame = subject.decode_frame(proof.review_bytes)
        for tick in (True, frame["issued_tick"] - 1, frame["expires_tick"]):
            with self.assertRaises(ValueError):
                replace(proof, completed_tick=tick)
        with self.assertRaises(ValueError):
            replace(proof, after=replace(proof.after, parents=(("D:\\", 1, 999),)))

    def test_unknown_write_never_gets_success_proof_or_rollback(self):
        self.submit()
        self.store.failure = "after_unknown"
        self.assertEqual(self.workflow.review_local(RID)["status"], "OUTCOME_UNKNOWN")
        self.assertNotIn("write_proof", self.workflow._records[RID])
        self.assertEqual(self.store.writes, 1)

    def test_owner_recheck_latency_after_write_cannot_extend_deadline(self):
        original = self.owner.binding
        def delayed():
            if self.store.writes:
                self.now = 55.0
            return original()
        self.owner.binding = delayed
        self.submit()
        self.assertEqual(self.workflow.review_local(RID)["status"], "OUTCOME_UNKNOWN")
        self.assertNotIn("write_proof", self.workflow._records[RID])

    def _create_fixture(self):
        store = subject.DevelopmentExclusiveCreateStore.__new__(subject.DevelopmentExclusiveCreateStore)
        before = subject.SourceSnapshot(None, None, PARENTS)
        store.snapshot = Mock(return_value=before)
        stream = MagicMock()
        stream.__enter__.return_value = stream
        stream.write.return_value = 1
        info = SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=1, st_dev=1, st_ino=101)
        return store, before, stream, info

    def test_expiry_during_exclusive_open_writes_zero_and_retains_reservation(self):
        store, before, stream, info = self._create_fixture()
        checks = Mock(side_effect=[None, subject.SourceConflict("expired after open")])
        with patch.object(Path, "open", return_value=stream), patch("os.fstat", return_value=info), \
                patch.object(subject, "_chain", return_value=PARENTS), \
                patch.object(Path, "unlink", side_effect=AssertionError("rollback forbidden")):
            with self.assertRaises(OSError):
                store.commit(TARGET, before, b"x", checks)
        checks.assert_called_with(check_snapshot=False)
        stream.write.assert_not_called()
        stream.__exit__.assert_called_once()

    def test_flush_loss_does_not_remove_or_retry_partial_file(self):
        store, before, stream, info = self._create_fixture()
        stream.flush.side_effect = OSError("flush lost")
        with patch.object(Path, "open", return_value=stream) as opened, \
                patch("os.fstat", return_value=info), patch.object(subject, "_chain", return_value=PARENTS), \
                patch.object(Path, "unlink", side_effect=AssertionError("rollback forbidden")):
            with self.assertRaises(OSError):
                store.commit(TARGET, before, b"x", Mock())
        opened.assert_called_once_with("xb")
        stream.write.assert_called_once_with(b"x")

    def test_invalid_direct_create_input_rejected_before_any_open(self):
        store, before, stream, info = self._create_fixture()
        for data in ("x", bytearray(b"x"), b"x" * 16385, b"\0", b"\xff"):
            with self.subTest(data=repr(data)[:20]), patch.object(Path, "open") as opened:
                with self.assertRaises((ValueError, UnicodeError)):
                    store.commit(TARGET, before, data, Mock())
                opened.assert_not_called()

    def test_post_fsync_cancellation_becomes_unknown_even_when_bytes_written(self):
        store, before, stream, info = self._create_fixture()
        checks = Mock(side_effect=[None, None, subject.SourceConflict("cancelled after fsync")])
        with patch.object(Path, "open", return_value=stream), patch("os.fstat", return_value=info), \
                patch.object(subject, "_chain", return_value=PARENTS), patch("os.fsync"):
            with self.assertRaises(OSError):
                store.commit(TARGET, before, b"x", checks)
        stream.write.assert_called_once_with(b"x")
        self.assertEqual(checks.call_count, 3)


if __name__ == "__main__":
    unittest.main()
