"""Mock integration only. No temp files, SQLite, native UI/IPC or child jobs.

AtomicStore is an independent in-memory FS specification, not an OS adapter.
All process entrypoints are guarded during these tests, including SDK calls.
"""
import ast
import asyncio
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from sora_commander_next.broker_host import (
    BrokerHost, BrokerUnavailable, encode_frame, decode_frame, validate_source_arguments)
from sora_commander_next.development_broker import DevelopmentBroker, DevelopmentConnection
from sora_commander_next.source_review import (
    SourcePolicy, SourceSnapshot, SourceReviewWorkflow, SourceConflict,
    ReadOnlySourceStore, DevelopmentExclusiveCreateStore)
from sora_commander_next.product_composition import build_product_host
from sora_commander_next.mcp_adapter import build_server, serve_stdio
from sora_commander_next.file_reads import _sensitive_component

ROOT = Path(__file__).resolve().parents[1]
# The read boundary refuses a path with a protected or dotted name, such as Temp, by design.
CHECKOUT_READABLE = not any(_sensitive_component(part) or part.startswith(".")
                            for part in ROOT.parts[1:])
ALLOW = r"D:\ApprovedDev"
TARGET = ALLOW + r"\src\feature.py"
OWNER = ("S-1-5-21-1-2-3-1001", (4, 5), 1)
PARENTS = ((ALLOW + r"\src", 1, 20), (ALLOW, 1, 10), ("D:\\", 1, 1))
RID = "1" * 32
SID = "a" * 48


class AtomicStore:
    def __init__(self):
        self.files = {}
        self.parents = PARENTS
        self.serial = 100
        self.writes = 0
        self.race = None
        self.failure = None
        self.lock = RLock()

    def snapshot(self, path):
        with self.lock:
            data, ino = self.files.get(path, (None, None))
            return SourceSnapshot(data, None if data is None else (1, ino, len(data), 1, 1), self.parents)

    def seed(self, path, data):
        self.serial += 1
        self.files[path] = (data, self.serial)

    def commit(self, path, before, data, last_check):
        with self.lock:
            if self.race:
                race, self.race = self.race, None
                race()
            if self.snapshot(path) != before:
                raise SourceConflict("atomic identity+bytes+parent CAS lost")
            last_check()
            if self.failure == "before_unknown":
                raise OSError("outcome unknowable")
            self.seed(path, data)
            self.writes += 1
            if self.failure == "after_unknown":
                raise OSError("lost write ACK")
            if self.failure == "bad_proof":
                self.seed(path, b"corrupted")
            return self.snapshot(path)


class Owner:
    def __init__(self):
        self.identity = OWNER
        self.active = True
        self.choice = True
        self.reviews = []
        self.on_review = None

    def verify(self):
        if not self.active:
            raise PermissionError("fixture locked")
        return True

    def binding(self):
        self.verify()
        return self.identity

    def review(self, raw, last_check):
        self.reviews.append(raw)
        if self.on_review:
            self.on_review()
        return self.choice


class Queue:
    def __init__(self):
        self.work = []
    def submit(self, callback):
        self.work.append(callback)
    def run(self):
        while self.work:
            self.work.pop(0)()


class Manager:
    """Independent mock of the real JobManager's bounded live-page schema."""
    DEV_SIMULATION_ONLY = True

    def __init__(self, events):
        self.events = events
        self.started = []
        self.closed = 0
        self.output = b"partial\n"
        self.state = "RUNNING"
        self.code = None
    def start(self, command):
        self.events.append("spawn")
        self.started.append(command)
        return {"status": "RUNNING", "request_id": command.request_id, "session_id": SID}
    def read_live_page(self, session_id, offset):
        data = self.output[offset:offset + 4096]
        return {"request_id": self.started[0].request_id, "session_id": session_id,
            "status": self.state, "exit_code": self.code, "offset": offset,
            "next_offset": offset + len(data), "captured_bytes": len(self.output),
            "output_incomplete": False, "overflow": False,
            "end_of_capture": offset + len(data) == len(self.output),
            "stdout_base64": base64.b64encode(data).decode(), "stderr_base64": ""}
    def close(self):
        self.closed += 1
    def confirm_terminal_readonly(self, rid, sid):
        return self.state == "COMPLETED"
    def stop(self, sid):
        return {"session_id": sid, "status": "CANCELLING"}


class Ledger:
    def __init__(self, events):
        self.events, self.records = events, []
        self.result = "DEV_RECORDED_ONCE"
        self.on_record = None
    def record_development_once(self, raw):
        self.events.append("commit_fixture")
        self.records.append(raw)
        if self.on_record:
            self.on_record()
        return self.result


class GuardedTests(unittest.TestCase):
    def setUp(self):
        for target in ("subprocess.Popen", "subprocess.run", "os.system", "os.startfile"):
            guard = patch(target, side_effect=AssertionError("live process forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.owner, self.store = Owner(), AtomicStore()
        self.now = 10.0
        self.policy = SourcePolicy((ALLOW,))
        self.workflow = SourceReviewWorkflow(policy=self.policy, store=self.store,
            owner=self.owner, clock=lambda: self.now)

    def create(self, content="x = 1\r\n", rid=RID):
        return {"path": TARGET, "content": content, "expected_new_sha256": sha256(content.encode()).hexdigest(),
                "request_id": rid}

    def patch_args(self, *, rid=RID):
        return {"path": TARGET, "old_text": "x = 1", "new_text": "x = 2",
            "expected_sha256": sha256(b"x = 1\r\n").hexdigest(),
            "expected_new_sha256": sha256(b"x = 2\r\n").hexdigest(), "request_id": rid}

    def submit(self):
        return self.workflow.submit("request_source_create", self.create())


class SourceWorkflowTests(GuardedTests):
    def test_exact_complete_review_and_create_proof(self):
        self.assertEqual(self.submit()["status"], "REVIEW")
        self.assertEqual(self.store.writes, 0)
        result = self.workflow.review_local(RID)
        self.assertEqual(result["status"], "CREATED_LOCAL_DEV")
        raw = self.owner.reviews[0]
        frame = decode_frame(raw)
        self.assertLessEqual(len(raw), 8192)
        self.assertEqual(frame["after_utf8"], "x = 1\r\n")
        self.assertEqual(frame["before_sha256"], None)
        self.assertEqual(frame["path"], TARGET)
        self.assertEqual(frame["operation"], "create")
        self.assertEqual(frame["owner_sid"], OWNER[0])
        self.assertEqual(frame["logon_luid"], list(OWNER[1]))
        self.assertEqual(frame["wts_session"], OWNER[2])
        self.assertEqual(len(frame["nonce"]), 64)
        self.assertEqual(frame["expires_tick"] - frame["issued_tick"], 45000)
        self.assertIn(b"\\r\\n", raw)
        self.assertEqual(result["sha256"], sha256(self.store.snapshot(TARGET).data).hexdigest())
        self.assertEqual(result["review_sha256"], sha256(raw).hexdigest())

    def test_literal_patch_binds_identity_before_after_and_diff(self):
        self.store.seed(TARGET, b"x = 1\r\n")
        before = self.store.snapshot(TARGET)
        self.workflow.submit("request_source_patch", self.patch_args())
        result = self.workflow.review_local(RID)
        self.assertEqual(result["status"], "PATCHED_LOCAL_DEV")
        frame = decode_frame(self.owner.reviews[0])
        self.assertEqual(frame["identity"], list(before.identity))
        self.assertEqual(frame["before_utf8"], "x = 1\r\n")
        self.assertEqual(frame["after_utf8"], "x = 2\r\n")
        self.assertIn("-x = 1\r\n", frame["diff"])
        self.assertEqual(self.store.snapshot(TARGET).data, b"x = 2\r\n")

    def test_rejection_consumes_once_and_writes_zero(self):
        self.owner.choice = False
        self.submit()
        self.assertEqual(self.workflow.review_local(RID)["status"], "DENIED")
        self.owner.choice = True
        self.workflow.review_local(RID)
        self.submit()
        self.assertEqual(len(self.owner.reviews), 1)
        self.assertEqual(self.store.writes, 0)

    def test_cancellation_during_review_cannot_write_or_replay(self):
        self.submit()
        self.owner.on_review = lambda: self.workflow.cancel(RID)
        self.assertEqual(self.workflow.review_local(RID)["status"], "DENIED")
        self.assertEqual(self.store.writes, 0)
        self.workflow.review_local(RID)
        self.assertEqual(len(self.owner.reviews), 1)

    def test_cancel_before_scheduler_creates_no_dialog(self):
        self.submit()
        self.assertEqual(self.workflow.cancel(RID)["status"], "CANCELLED")
        self.workflow.review_local(RID)
        self.assertEqual(self.owner.reviews, [])
        self.assertEqual(self.store.writes, 0)

    def test_owner_lock_during_dialog_denies_with_zero_writes(self):
        self.submit()
        self.owner.on_review = lambda: setattr(self.owner, "active", False)
        self.assertEqual(self.workflow.review_local(RID)["status"], "DENIED")
        self.assertEqual(self.store.writes, 0)

    def test_owner_tuple_change_denies_every_dimension(self):
        for identity in (("S-1-5-21-9-2-3-1001", OWNER[1], 1), (OWNER[0], (9, 5), 1),
                         (OWNER[0], OWNER[1], 2)):
            with self.subTest(identity=identity):
                self.set_identity_workflow()
                self.submit()
                self.owner.on_review = lambda: setattr(self.owner, "identity", identity)
                self.assertEqual(self.workflow.review_local(RID)["status"], "DENIED")
                self.assertEqual(self.store.writes, 0)

    def set_identity_workflow(self):
        self.owner = Owner()
        self.workflow = SourceReviewWorkflow(policy=self.policy, store=self.store,
            owner=self.owner, clock=lambda: self.now)

    def test_expiry_exact_boundary_is_denied(self):
        self.submit()
        self.owner.on_review = lambda: setattr(self, "now", 55.0)
        self.assertEqual(self.workflow.review_local(RID)["status"], "DENIED")
        self.assertEqual(self.store.writes, 0)

    def test_clock_rollback_denies_and_halts_admission(self):
        self.submit()
        self.now = 9.0
        self.assertEqual(self.workflow.review_local(RID)["status"], "DENIED")
        self.assertEqual(self.workflow.submit("request_source_create", self.create(rid="2" * 32))["status"], "DENIED")

    def test_same_hash_different_identity_denies(self):
        self.store.seed(TARGET, b"x = 1\r\n")
        self.workflow.submit("request_source_patch", self.patch_args())
        self.owner.on_review = lambda: self.store.seed(TARGET, b"x = 1\r\n")
        self.assertEqual(self.workflow.review_local(RID)["status"], "DENIED")
        self.assertEqual(self.store.writes, 0)

    def test_changed_bytes_or_parent_during_review_denies(self):
        self.submit()
        self.owner.on_review = lambda: setattr(self.store, "parents", ((ALLOW, 1, 99),))
        self.assertEqual(self.workflow.review_local(RID)["status"], "DENIED")
        self.assertEqual(self.store.writes, 0)

    def test_atomic_race_between_check_and_commit_writes_zero(self):
        self.submit()
        self.store.race = lambda: self.store.seed(TARGET, b"other writer")
        self.assertEqual(self.workflow.review_local(RID)["status"], "DENIED")
        self.assertEqual(self.store.snapshot(TARGET).data, b"other writer")
        self.assertEqual(self.store.writes, 0)

    def test_unknown_commit_is_terminal_no_retry_no_rollback(self):
        self.submit()
        self.store.failure = "after_unknown"
        result = self.workflow.review_local(RID)
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertIs(result["auto_rollback"], False)
        self.store.failure = None
        self.workflow.review_local(RID)
        self.assertEqual(self.store.writes, 1)
        self.assertEqual(self.workflow.submit("request_source_create", self.create(rid="2" * 32))["status"], "DENIED")

    def test_bad_post_write_proof_is_unknown(self):
        self.submit()
        self.store.failure = "bad_proof"
        self.assertEqual(self.workflow.review_local(RID)["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(self.store.writes, 1)

    def test_request_id_conflict_and_mutable_input_detached(self):
        args = self.create()
        self.workflow.submit("request_source_create", args)
        args["content"] = "bad"
        self.assertEqual(self.workflow.submit("request_source_create", self.create("different"))["status"], "DENIED")
        self.workflow.review_local(RID)
        self.assertEqual(self.store.snapshot(TARGET).data, b"x = 1\r\n")

    def test_stale_digest_and_ambiguous_anchor_refuse_before_review(self):
        self.store.seed(TARGET, b"x = 1\r\nx = 1\r\n")
        args = self.patch_args()
        args["expected_sha256"] = sha256(self.store.snapshot(TARGET).data).hexdigest()
        with self.assertRaises(SourceConflict):
            self.workflow.submit("request_source_patch", args)
        self.assertEqual(self.owner.reviews, [])

    def test_new_digest_mismatch_refuses_before_review(self):
        args = self.create()
        args["expected_new_sha256"] = "0" * 64
        with self.assertRaises(SourceConflict):
            self.workflow.submit("request_source_create", args)

    def test_wire_and_full_review_limits_refuse_no_truncation(self):
        with self.assertRaises(ValueError):
            self.workflow.submit("request_source_create", self.create("あ" * 1600))
        # Input fits, but before+after+diff+identity in complete review do not.
        with self.assertRaises(ValueError):
            self.workflow.submit("request_source_create", self.create("x\n" * 1800))
        self.assertEqual(self.owner.reviews, [])
        self.assertEqual(self.store.writes, 0)

    def test_policy_exact_roots_and_windows_alias_negatives(self):
        for path in (r"D:\ApprovedDevOther\a.py", r"D:\a.py", r"d:\ApprovedDev\a.py",
                     r"D:\APPROVEDDEV\a.py", r"D:\ApprovedDev\..\a.py", r"D:\ApprovedDev\.git\a.py",
                     r"D:\ApprovedDev\BUILD\a.py", r"D:\ApprovedDev\src\a.py:ads",
                     r"D:\ApprovedDev\src\CON.py", r"D:\ApprovedDev\src\a.exe",
                     r"D:\ApprovedDev\src\a~1.py", r"D:\ApprovedDev\\src\a.py",
                     r"\\server\share\a.py", r"\\?\D:\ApprovedDev\a.py", "D:/ApprovedDev/a.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.policy.target(path)
        with self.assertRaises(ValueError):
            SourcePolicy(("D:\\",))
        with self.assertRaises(ValueError):
            SourcePolicy().target(TARGET)
        self.assertEqual(self.policy.target(TARGET), TARGET)

    def test_malformed_wire_types_and_no_remote_approve(self):
        for update in ({"approved": True}, {"content": True}, {"expected_new_sha256": "A" * 64},
                       {"request_id": True}, {"content": "\ud800"}):
            with self.subTest(update=update), self.assertRaises((ValueError, UnicodeError)):
                validate_source_arguments("request_source_create", {**self.create(), **update})

    def test_disk_patch_backend_refuses_without_opening_or_replacing(self):
        before = SourceSnapshot(b"x", (1, 1, 1, 1, 1), PARENTS)
        for cls in (ReadOnlySourceStore, DevelopmentExclusiveCreateStore):
            store = cls.__new__(cls)
            with patch.object(Path, "open", side_effect=AssertionError("must not open")), \
                    patch("os.replace", side_effect=AssertionError("must not replace")), \
                    self.assertRaises(SourceConflict):
                store.commit(TARGET, before, b"y", Mock())

    def test_exclusive_create_post_open_conflict_is_unknown_not_zero_write(self):
        from unittest.mock import MagicMock
        from sora_commander_next import source_review
        before = SourceSnapshot(None, None, PARENTS)
        store = DevelopmentExclusiveCreateStore.__new__(DevelopmentExclusiveCreateStore)
        store.snapshot = Mock(side_effect=[before, SourceConflict("post-open path changed")])
        stream = MagicMock()
        stream.__enter__.return_value = stream
        stream.write.return_value = 1
        info = SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=1, st_dev=1, st_ino=101)
        with patch.object(Path, "open", return_value=stream) as opened, \
                patch.object(source_review, "_chain", return_value=PARENTS), \
                patch("os.fstat", return_value=info), patch("os.fsync"), \
                patch.object(Path, "unlink", side_effect=AssertionError("rollback forbidden")), \
                self.assertRaises(OSError):
            store.commit(TARGET, before, b"x", lambda **kwargs: None)
        opened.assert_called_once_with("xb")
        stream.write.assert_called_once_with(b"x")

    def test_create_lost_exclusive_race_writes_zero(self):
        store = DevelopmentExclusiveCreateStore.__new__(DevelopmentExclusiveCreateStore)
        before = SourceSnapshot(None, None, PARENTS)
        store.snapshot = Mock(return_value=before)
        with patch.object(Path, "open", side_effect=FileExistsError) as opened, self.assertRaises(SourceConflict):
            store.commit(TARGET, before, b"x", lambda: None)
        opened.assert_called_once_with("xb")

    def test_hidden_source_and_reparse_ancestors_denied_volume_metadata_allowed(self):
        from sora_commander_next.source_review import _chain
        for attributes in (0x12, 0x410):
            info = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=attributes, st_dev=1, st_ino=1)
            with self.subTest(attributes=attributes), patch.object(Path, "lstat", return_value=info), self.assertRaises(SourceConflict):
                _chain(Path(ALLOW))
        info = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x16, st_dev=1, st_ino=1)
        with patch.object(Path, "lstat", return_value=info):
            self.assertEqual(len(_chain(Path("D:\\"))), 1)

    def test_review_reentrancy_is_busy_and_not_a_second_dialog(self):
        self.submit()
        nested = []
        self.owner.on_review = lambda: nested.append(self.workflow.review_local(RID))
        self.workflow.review_local(RID)
        self.assertEqual(nested, [{"status": "BUSY"}])
        self.assertEqual(self.store.writes, 1)

    def test_terminal_integrity_evidence_is_detached_from_return_value(self):
        self.submit()
        result = self.workflow.review_local(RID)
        identity = list(result["identity"])
        result["identity"][1] = 999
        self.assertEqual(self.workflow.status(RID)["identity"], identity)

    def test_history_capacity_denies_without_evicting_spent_consent(self):
        for i in range(64):
            self.workflow.submit("request_source_create", self.create(rid=f"{i:032x}"))
        result = self.workflow.submit("request_source_create", self.create(rid="f" * 32))
        self.assertEqual(result["status"], "DENIED")
        self.assertEqual(len(self.workflow._records), 64)
        self.assertEqual(self.store.writes, 0)

    def test_unknown_review_with_locked_owner_is_denied_not_internal_error(self):
        self.owner.active = False
        self.assertEqual(self.workflow.review_local(RID)["status"], "DENIED")


class IntegrationTests(GuardedTests):
    def setUp(self):
        super().setUp()
        self.events, self.queue = [], Queue()
        self.manager = Manager(self.events)
        self.factory = Mock(return_value=self.manager)
        self.ledger = Ledger(self.events)
        self.command_review = Mock(return_value=True)
        self.broker = DevelopmentBroker(source_workflow=self.workflow, owner=self.owner,
            scheduler=self.queue, manager_factory=self.factory, ledger=self.ledger,
            command_review=self.command_review, executable=str(ROOT / '.venv/Scripts/python.exe'),
            cwd=str(ROOT), clock=lambda: self.now)
        self.host = BrokerHost(DevelopmentConnection(self.broker))

    def test_real_job_manager_cannot_be_injected_into_dev_fixture_broker(self):
        from sora_commander_next.jobs import JobManager
        # Construct without __init__: the negative test itself creates no
        # Job Object, subprocess or native OS resource.
        inert_native = object.__new__(JobManager)
        with self.assertRaises(TypeError):
            DevelopmentBroker(source_workflow=self.workflow, owner=self.owner,
                scheduler=self.queue, manager_factory=lambda: inert_native,
                ledger=self.ledger, command_review=self.command_review,
                executable=str(ROOT / '.venv/Scripts/python.exe'),
                cwd=str(ROOT), clock=lambda: self.now)
        with self.assertRaises(TypeError):
            DevelopmentBroker(source_workflow=self.workflow, owner=self.owner,
                scheduler=self.queue, manager_factory=JobManager,
                ledger=self.ledger, command_review=self.command_review,
                executable=str(ROOT / '.venv/Scripts/python.exe'),
                cwd=str(ROOT), clock=lambda: self.now)
        with self.assertRaises(TypeError):
            DevelopmentBroker(source_workflow=self.workflow, owner=self.owner,
                scheduler=self.queue, manager_factory=lambda: object(),
                ledger=self.ledger, command_review=self.command_review,
                executable=str(ROOT / '.venv/Scripts/python.exe'),
                cwd=str(ROOT), clock=lambda: self.now)

    def job_args(self):
        return {"executable": str(ROOT / '.venv/Scripts/python.exe'), "argv": ["--version"],
                "cwd": str(ROOT), "request_id": "b" * 32, "timeout": 200, "output_limit": 65536}

    def test_sdk_create_patch_then_job_reconnect_same_manager(self):
        sdk = build_server(self.host)
        def call(name, args):
            return asyncio.run(sdk.call_tool(name, args))[1]
        self.assertEqual(call("request_source_create", self.create())["status"], "REVIEW")
        self.assertEqual(self.store.writes, 0)
        self.queue.run()
        self.assertEqual(call("source_review_status", {"request_id": RID})["status"], "CREATED_LOCAL_DEV")
        self.assertEqual(call("request_source_patch", self.patch_args(rid="2" * 32))["status"], "REVIEW")
        self.queue.run()
        self.assertEqual(self.store.snapshot(TARGET).data, b"x = 2\r\n")
        self.assertEqual(call("run_command", {**self.job_args(), "background": True})["status"], "REVIEW")
        self.assertEqual(self.manager.started, [])
        self.queue.run()
        self.assertEqual(self.events, ["commit_fixture", "spawn"])
        self.host.close()
        self.assertEqual(self.manager.closed, 0)
        self.host = BrokerHost(DevelopmentConnection(self.broker))
        snapshot = self.host.request_status("b" * 32)
        self.assertEqual(snapshot["status"], "RUNNING")
        self.assertEqual(base64.b64decode(snapshot["stdout_base64"]), b"partial\n")
        self.manager.state, self.manager.code = "COMPLETED", 0
        self.manager.output += b"done\n"
        final = self.host.read_process_output(SID, 8)
        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(final["exit_code"], 0)
        self.assertEqual(base64.b64decode(final["stdout_base64"]), b"done\n")
        self.assertEqual(len(self.manager.started), 1)
        self.factory.assert_called_once_with()

    def test_source_duplicate_transport_does_not_schedule_again(self):
        self.host.request_source_create(**self.create())
        self.host.request_source_create(**self.create())
        self.assertEqual(len(self.queue.work), 1)
        self.queue.run()
        self.host.request_source_create(**self.create())
        self.assertEqual(len(self.queue.work), 0)
        self.assertEqual(self.store.writes, 1)

    def test_job_untrusted_receipt_dialects_never_start(self):
        for status in ("DATA_RECORDED_ONCE", "DATA_UNTRUSTED_FRESH", "COMMITTED_FRESH", "UNKNOWN", True):
            with self.subTest(status=status):
                args = self.job_args()
                args["request_id"] = sha256(str(status).encode()).hexdigest()[:32]
                self.ledger.result = status
                self.host.run_command(background=True, **args)
                self.queue.run()
        self.assertEqual(self.manager.started, [])

    def test_late_ledger_ack_no_start_no_retry(self):
        self.ledger.on_record = lambda: setattr(self, "now", 55.0)
        self.host.run_command(background=True, **self.job_args())
        self.queue.run()
        self.now = 10
        self.host.run_command(background=True, **self.job_args())
        self.queue.run()
        self.assertEqual(len(self.ledger.records), 1)
        self.assertEqual(self.manager.started, [])

    def test_owner_change_after_ledger_never_starts(self):
        self.ledger.on_record = lambda: setattr(self.owner, "identity", (OWNER[0], (9, 5), 1))
        self.host.run_command(background=True, **self.job_args())
        self.queue.run()
        self.assertEqual(self.manager.started, [])

    def test_spawn_ack_loss_does_not_reschedule(self):
        original = self.manager.start
        def uncertain(command):
            original(command)
            raise OSError("ACK lost")
        self.manager.start = uncertain
        self.host.run_command(background=True, **self.job_args())
        self.queue.run()
        self.host.run_command(background=True, **self.job_args())
        self.queue.run()
        self.assertEqual(len(self.manager.started), 1)
        self.assertEqual(len(self.ledger.records), 1)

    def test_foreign_session_cannot_read_or_cancel(self):
        self.assertEqual(self.host.read_process_output("c" * 48)["status"], "DENIED")
        self.assertEqual(self.host.stop_process("c" * 48)["status"], "DENIED")

    def test_pending_review_survives_host_disconnect(self):
        self.host.request_source_create(**self.create())
        self.host.close()
        self.queue.run()
        second = BrokerHost(DevelopmentConnection(self.broker))
        self.assertEqual(second.source_review_status(RID)["status"], "CREATED_LOCAL_DEV")
        self.assertEqual(self.manager.closed, 0)

    def test_mcp_rejects_remote_approval_fields_before_dispatch(self):
        from mcp.server.fastmcp.exceptions import ToolError
        sdk = build_server(self.host)
        with self.assertRaises(ToolError):
            asyncio.run(sdk.call_tool("request_source_create", {**self.create(), "approved": True}))
        self.assertEqual(self.queue.work, [])


class ProductBoundaryTests(GuardedTests):
    def test_factory_is_unavailable_and_has_no_fake_identity_parameters(self):
        host = build_product_host()
        self.assertEqual(host.run_command(**{"executable": "x"})["status"], "UNAVAILABLE")
        self.assertEqual(host.request_source_create(**self.create())["status"], "UNAVAILABLE")
        self.assertEqual(host.status()["service_identity"], "NOT_AUTHENTICATED")
        for name in ("jobs", "worker", "approval", "_owner_review_dispatcher"):
            self.assertFalse(hasattr(host, name))
        with self.assertRaises(TypeError):
            build_product_host(service_verified=True)

    def test_stdio_product_selection_cannot_construct_legacy_host(self):
        from sora_commander_next import stdio_main
        with patch.dict(os.environ, {"SORA_NEXT_PRODUCT_BROKER": "1"}, clear=True), \
                patch.object(stdio_main, "Host", side_effect=AssertionError("legacy Host")), \
                patch.object(stdio_main, "serve_stdio") as serve:
            stdio_main.main()
            self.assertIs(type(serve.call_args.args[0]), BrokerHost)

    def test_stdio_product_and_legacy_flags_fail_before_start(self):
        from sora_commander_next import stdio_main
        for name in ("SORA_NEXT_OWNER_REVIEW_PILOT", "SORA_NEXT_EDIT_PILOT", "SORA_NEXT_CREATE_PILOT"):
            with self.subTest(name=name), patch.dict(os.environ,
                    {"SORA_NEXT_PRODUCT_BROKER": "1", name: "1"}, clear=True), \
                    patch.object(stdio_main, "serve_stdio") as serve, self.assertRaises(RuntimeError):
                stdio_main.main()
            serve.assert_not_called()

    def test_stdio_eof_closes_connection_only_even_on_error(self):
        transport = Mock()
        host = BrokerHost(transport)
        server = Mock()
        server.run.side_effect = OSError("EOF fixture")
        with patch("sora_commander_next.mcp_adapter.build_server", return_value=server), self.assertRaises(OSError):
            serve_stdio(host)
        host.close()
        transport.close.assert_called_once_with()
        transport.request.assert_not_called()

    def test_legacy_owner_controller_refuses_transport_host_before_native_guard(self):
        from sora_commander_next.owner_ui import OwnerController
        from sora_commander_next.windows_owner_controller import WindowsOwnerController
        with patch("sora_commander_next.windows_owner_controller.OwnerSessionGuard",
                   side_effect=AssertionError("native guard must not construct")):
            for cls in (OwnerController, WindowsOwnerController):
                with self.subTest(cls=cls), self.assertRaises(PermissionError):
                    cls(build_product_host())

    def test_product_sdk_tools_forward_unavailable_and_pilots_absent(self):
        sdk = build_server(build_product_host())
        names = {tool.name for tool in sdk._tool_manager.list_tools()}
        self.assertIn("request_source_patch", names)
        self.assertNotIn("request_text_edit", names)
        self.assertNotIn("request_text_create", names)
        _, row = asyncio.run(sdk.call_tool("request_source_create", self.create()))
        self.assertEqual(row["status"], "UNAVAILABLE")

    def test_transport_failure_never_retries_or_falls_back(self):
        transport = Mock()
        for error, expected in ((BrokerUnavailable(), "UNAVAILABLE"), (TimeoutError(), "OUTCOME_UNKNOWN")):
            transport.request.side_effect = error
            before = transport.request.call_count
            result = BrokerHost(transport).request_source_create(**self.create())
            self.assertEqual(result["status"], expected)
            self.assertEqual(transport.request.call_count, before + 1)

    def test_reply_correlation_type_duplicate_and_size_checks(self):
        for mutation in (lambda x: {**x, "call_id": "bad"}, lambda x: {**x, "v": True},
                         lambda x: {**x, "result": {"status": "COMMITTED_FRESH"}}):
            def reply(raw):
                request = decode_frame(raw)
                return encode_frame(mutation({"v": 1, "action": request["action"],
                    "call_id": request["call_id"], "result": {"status": "OK"}}))
            result = BrokerHost(Mock(request=reply)).request_status(RID)
            self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        for raw in (b'{"v":1,"v":1}', b"x" * 8193, b'{ "v":1}', b'{"v":NaN}'):
            with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                decode_frame(raw)

    def test_oversize_request_is_rejected_before_transport(self):
        transport = Mock()
        result = BrokerHost(transport).request_source_create(**self.create("x" * 9000))
        self.assertEqual(result["status"], "DENIED")
        transport.request.assert_not_called()

    @unittest.skipUnless(CHECKOUT_READABLE, "checkout path has a protected name")
    def test_real_approved_repo_source_read_through_product_sdk_is_read_only(self):
        path = ROOT / "src/sora_commander_next/commands.py"
        before = path.read_bytes()
        host = build_product_host(read_roots=(str(path.parent),))
        _, result = asyncio.run(build_server(host).call_tool("read_text_file", {"path": str(path)}))
        self.assertEqual(result["content"].encode("utf-8"), before)
        self.assertEqual(path.read_bytes(), before)
        store = ReadOnlySourceStore(SourcePolicy((str(path.parent),)))
        self.assertEqual(store.snapshot(str(path)).data, before)

    @unittest.skipUnless(CHECKOUT_READABLE, "checkout path has a protected name")
    def test_actual_source_preview_and_product_refusal_leave_bytes_unchanged(self):
        path = ROOT / "src/sora_commander_next/commands.py"
        before = path.read_bytes()
        sdk = build_server(build_product_host(read_roots=(str(path.parent),)))
        arguments = {"path": str(path), "old_text": "class Command:",
            "new_text": "class CandidateCommand:", "expected_sha256": sha256(before).hexdigest()}
        _, preview = asyncio.run(sdk.call_tool("preview_source_patch", arguments))
        self.assertEqual(preview["status"], "PREVIEW_ONLY")
        # The MCP preview omits whole content; recompute the reviewed digest.
        self.assertNotIn("content", preview)
        self.assertEqual(preview["new_sha256"], sha256(before.replace(
            b"class Command:", b"class CandidateCommand:", 1)).hexdigest())
        _, result = asyncio.run(sdk.call_tool("request_source_patch", {**arguments,
            "expected_new_sha256": preview["new_sha256"], "request_id": RID}))
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(path.read_bytes(), before)

    def test_inventory_and_transitive_product_imports_exclude_development_code(self):
        from sora_commander_next.release_inventory import (
            MODULES, DEV_ONLY_MODULES, OPTIONAL_DEV_ONLY_MODULES)
        package = ROOT / "src/sora_commander_next"
        actual = {p.name for p in package.glob("*.py")} - OPTIONAL_DEV_ONLY_MODULES
        self.assertEqual(actual, set(MODULES) | (DEV_ONLY_MODULES - OPTIONAL_DEV_ONLY_MODULES))
        self.assertFalse(set(MODULES) & DEV_ONLY_MODULES)
        for name in ("source_review.py", "development_broker.py"):
            self.assertIn(name, DEV_ONLY_MODULES)
        pending = ["product_composition.py", "broker_host.py"]
        seen = set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            self.assertIn(name, MODULES)
            self.assertNotIn(name, DEV_ONLY_MODULES)
            tree = ast.parse((package / name).read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                    target = node.module + ".py"
                    pending.append(target)
        self.assertNotIn("jobs.py", seen)
        self.assertNotIn("host.py", seen)
        self.assertNotIn("windows_owner.py", seen)


class UnacceptedOSCases(unittest.TestCase):
    @unittest.skip("NOT_ACCEPTED: real repo-local temporary create/staging fixture environment previously denied; no alternate path retry")
    def test_real_atomic_create_and_disk_integrity(self):
        pass

    @unittest.skip("NOT_ACCEPTED: native strong patch CAS, hostile directory races and ACL verification have no accepted adapter")
    def test_real_native_cas_patch(self):
        pass

    @unittest.skip("NOT_ACCEPTED: service, identity, pipe, Owner UI and real child job require separate Owner transaction")
    def test_live_chatgpt_owner_edit_job_host_restart(self):
        pass


if __name__ == "__main__":
    unittest.main()
