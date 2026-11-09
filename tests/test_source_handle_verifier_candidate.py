from dataclasses import replace
import difflib
from hashlib import sha256
from pathlib import PureWindowsPath
import unittest

from sora_commander_next.broker_host import encode_frame, decode_frame
from sora_commander_next.source_review import SourcePolicy, SourceSnapshot, SourceConflict
from sora_commander_next.source_handle_verifier_candidate import (
    HandleRecord, NativeSourceHandleVerifierCandidate,
)


class HandleBackend:
    def __init__(self, records, data):
        self.records, self.data = records, data
        self.opened, self.closed, self.samples = [], [], {}
        self.change, self.fail_close, self.on_close = None, None, None
        self.bad_handle = None
    def open_read_handle(self, path, access, share, creation, flags):
        self.opened.append((path, access, share, creation, flags))
        return self.bad_handle if self.bad_handle is not None else len(self.opened)
    def inspect_handle(self, handle):
        path = self.opened[handle-1][0]
        self.samples[path] = self.samples.get(path, 0) + 1
        record = self.records[path]
        return self.change(path, self.samples[path], record) if self.change else record
    def read_bounded(self, handle, maximum):
        if maximum != 16385:
            raise AssertionError("unbounded read")
        return self.data
    def close(self, handle):
        self.closed.append(handle)
        if self.on_close:
            self.on_close()
        return handle != self.fail_close
    def write(self, *args):
        raise AssertionError("writer must never be reached")
    replace = write


class SourceHandleVerifierCandidateTests(unittest.TestCase):
    def setUp(self):
        self.path = r"D:\Approved\src\main.py"
        self.old, self.new = b"value = 1\n", b"value = 2\n"
        self.owner = ("S-1-5-21-1-2-3-1001", (5, 6), 1)
        self.now = 100
        ancestors = tuple(str(p) for p in PureWindowsPath(self.path).parents)
        self.before = SourceSnapshot(self.old, (1, 8, len(self.old), 1, 2),
            tuple((p, 1, i+1) for i, p in enumerate(ancestors)))
        records = {p: HandleRecord("\\\\?\\"+p, (1, ident, 0, 1, 2), 0x10, 0, 1, True, False)
                   for p, _, ident in self.before.parents}
        records[self.path] = HandleRecord("\\\\?\\"+self.path, self.before.identity, 0, 0, 1, False, False)
        self.backend = HandleBackend(records, self.old)
        self.policy = SourcePolicy((r"D:\Approved",))
        self.verifier = NativeSourceHandleVerifierCandidate(self.policy, _backend=self.backend,
            _clock_ms=lambda: self.now)
        self.frame = dict(v=1, request_id="a"*32, path=self.path, operation="patch",
            before_sha256=sha256(self.old).hexdigest(), after_sha256=sha256(self.new).hexdigest(),
            before_utf8=self.old.decode(), after_utf8=self.new.decode(),
            diff="".join(difflib.unified_diff(self.old.decode().splitlines(keepends=True),
                self.new.decode().splitlines(keepends=True), fromfile="current", tofile="proposed")),
            identity=self.before.identity, parents=self.before.parents,
            owner_sid=self.owner[0], logon_luid=self.owner[1], wts_session=1,
            nonce="b"*64, issued_tick=1, expires_tick=45001)

    def verify(self, **kw):
        return self.verifier.verify_review_data_only(encode_frame(self.frame), self.before,
            current_owner=kw.get("current_owner", lambda: self.owner))

    def test_complete_dry_run_matches_but_commit_stays_denied(self):
        result = self.verify()
        self.assertEqual(result.status, "DATA_DRY_RUN_MATCH")
        self.assertEqual(result.authority, "NONE_DATA_ONLY")
        self.assertEqual(result.commit, "DENIED_NATIVE_EXCLUSION_NOT_ACCEPTED")
        self.assertEqual(result.review_sha256, sha256(encode_frame(self.frame)).hexdigest())
        with self.assertRaisesRegex(SourceConflict, "STRONG_SOURCE_CAS"):
            self.verifier.commit(self.path, self.before, self.new, lambda: True)

    def test_open_rights_share_zero_and_all_ancestors_retained(self):
        self.verify()
        self.assertEqual([row[0] for row in self.backend.opened],
            [p[0] for p in reversed(self.before.parents)] + [self.path])
        for path, access, share, creation, flags in self.backend.opened:
            self.assertEqual((share, creation), (0, 3))
            self.assertEqual(access, 0x81 if path == self.path else 0x80)
            self.assertEqual(flags, 0x200000 | (0 if path == self.path else 0x2000000))
        self.assertEqual(self.backend.closed, [4, 3, 2, 1])

    def test_default_backend_never_opens(self):
        self.verifier = NativeSourceHandleVerifierCandidate(self.policy)
        with self.assertRaisesRegex(SourceConflict, "NOT_ENABLED"):
            self.verify()
        self.assertEqual(self.backend.opened, [])

    def test_duplicate_nonce_terminal(self):
        self.verify()
        with self.assertRaises(SourceConflict):
            self.verify()
        self.assertEqual(len(self.backend.opened), 4)

    def test_reparse_or_hidden_ancestor_denied(self):
        for attribute in (0x410, 0x12):
            with self.subTest(attribute=attribute):
                self.setUp()
                path = r"D:\Approved"
                self.backend.records[path] = replace(self.backend.records[path], attributes=attribute)
                with self.assertRaises(SourceConflict):
                    self.verify()
                self.assertEqual(self.backend.closed, [2, 1])

    def test_hardlink_and_delete_pending_file_denied(self):
        for changes in ({"links": 2}, {"delete_pending": True}, {"reparse_tag": 1}):
            self.setUp()
            self.backend.records[self.path] = replace(self.backend.records[self.path], **changes)
            with self.assertRaises(SourceConflict):
                self.verify()
            self.assertEqual(self.backend.closed, [4, 3, 2, 1])

    def test_final_path_alias_denied(self):
        self.backend.records[self.path] = replace(self.backend.records[self.path], final_path=r"\\?\D:\Other\main.py")
        with self.assertRaises(SourceConflict):
            self.verify()

    def test_changed_opened_file_identity_denied(self):
        self.backend.records[self.path] = replace(self.backend.records[self.path], identity=(1, 9, len(self.old), 1, 2))
        with self.assertRaises(SourceConflict):
            self.verify()

    def test_changed_ancestor_identity_denied(self):
        path = r"D:\Approved"
        self.backend.records[path] = replace(self.backend.records[path], identity=(1, 99, 0, 1, 2))
        with self.assertRaises(SourceConflict):
            self.verify()

    def test_changed_file_or_ancestor_on_second_sample_denied(self):
        for target in (self.path, r"D:\Approved"):
            self.setUp()
            self.backend.change = lambda p, n, r: replace(r, identity=(1, 90, r.identity[2], 1, 2)) if p == target and n == 2 else r
            with self.assertRaises(SourceConflict):
                self.verify()

    def test_digest_matching_cannot_hide_changed_bytes(self):
        self.backend.data = b"value = 9\n"
        with self.assertRaises(SourceConflict):
            self.verify()
        count = len(self.backend.opened)
        with self.assertRaises(SourceConflict):
            self.verify()
        self.assertEqual(len(self.backend.opened), count)

    def test_oversized_or_non_bytes_read_denied(self):
        for data in (b"x"*16385, "value = 1\n"):
            self.setUp()
            self.backend.data = data
            with self.assertRaises(SourceConflict):
                self.verify()

    def test_owner_change_and_bool_luid_denied(self):
        for owner in ((self.owner[0], (True, 6), 1), (self.owner[0], (5, 6), 2)):
            self.setUp()
            with self.assertRaises(SourceConflict):
                self.verify(current_owner=lambda: owner)
            self.assertEqual(self.backend.opened, [])

    def test_owner_observer_latency_does_not_extend_deadline(self):
        def observer():
            self.now = 45001
            return self.owner
        with self.assertRaises(SourceConflict):
            self.verify(current_owner=observer)
        self.assertEqual(self.backend.opened, [])

    def test_late_close_cannot_produce_dry_run_success(self):
        self.backend.on_close = lambda: setattr(self, "now", 45001)
        with self.assertRaises(SourceConflict):
            self.verify()
        self.assertEqual(len(self.backend.closed), 4)

    def test_close_failure_latches_all_future_reviews(self):
        self.backend.fail_close = 4
        with self.assertRaises(OSError):
            self.verify()
        self.assertEqual(self.backend.closed, [4, 3, 2, 1])
        self.frame["nonce"] = "c"*64
        with self.assertRaises(SourceConflict):
            self.verify()
        self.assertEqual(len(self.backend.opened), 4)

    def test_invalid_handles_not_closed(self):
        self.backend.bad_handle = True
        with self.assertRaises(SourceConflict):
            self.verify()
        self.assertEqual(self.backend.closed, [])

    def test_unapproved_root_and_incomplete_ancestor_chain_denied(self):
        self.frame["path"] = r"D:\Other\src\main.py"
        with self.assertRaises(ValueError):
            self.verify()
        self.setUp()
        self.before = replace(self.before, parents=self.before.parents[:-1])
        self.frame["parents"] = self.before.parents
        with self.assertRaises(SourceConflict):
            self.verify()

    def test_altered_review_hash_display_or_identity_denied(self):
        for field, value in (("before_sha256", "f"*64), ("diff", "looks safe"),
                             ("after_sha256", "f"*64), ("identity", [1, 999, 10, 1, 2])):
            self.setUp()
            self.frame[field] = value
            with self.assertRaises(SourceConflict):
                self.verify()
            self.assertEqual(self.backend.opened, [])

    def test_clock_rollback_denied_and_all_handles_closed(self):
        def change(p, n, record):
            self.now = 99
            return record
        self.backend.change = change
        with self.assertRaises(SourceConflict):
            self.verify()
        self.assertEqual(self.backend.closed, [1])

    def test_no_asserted_native_exclusion_can_enable_commit(self):
        with self.assertRaises(TypeError):
            NativeSourceHandleVerifierCandidate(self.policy, native_exclusion=True)

    def test_actual_source_workflow_review_schema_is_consumed_without_writing(self):
        from unittest.mock import Mock
        from test_sol_broker_vertical import Owner
        from sora_commander_next.source_review import SourceReviewWorkflow
        owner = Owner()
        owner.identity = self.owner
        store = Mock(snapshot=lambda path: self.before,
                     commit=Mock(side_effect=AssertionError("no writer")))
        workflow = SourceReviewWorkflow(policy=self.policy, store=store, owner=owner, clock=lambda: 0.1)
        workflow.submit("request_source_patch", dict(path=self.path, old_text="1", new_text="2",
            expected_sha256=sha256(self.old).hexdigest(),
            expected_new_sha256=sha256(self.new).hexdigest(), request_id="a"*32))
        raw = workflow._records["a"*32]["raw"]
        result = self.verifier.verify_review_data_only(raw, self.before, current_owner=lambda: self.owner)
        self.assertEqual(result.review_sha256, sha256(raw).hexdigest())
        self.assertEqual(result.commit, "DENIED_NATIVE_EXCLUSION_NOT_ACCEPTED")
        store.commit.assert_not_called()
