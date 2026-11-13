"""Synthetic activity/real-SDK/wire tests. No Owner UI, child, disk or archive."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from threading import Lock, RLock
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

from sora_commander_next.activity import (
    ActivityStore, InvalidMetadata, Record, arguments, publish, reply,
    validate_record, validate_reply, RECORD_KEYS, REPLY_KEYS,
)
from sora_commander_next.broker_host import BrokerHost, UnavailableTransport, decode_frame, encode_frame
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import build_server
from sora_commander_next.owner_broker_dev import DevBrokerHost, OwnerBrokerBackend
from sora_commander_next.owner_broker_service import OwnerBrokerService
from sora_commander_next.owner_review_dispatch import OwnerReviewDispatcher

RID, PID, SID = "1" * 32, "2" * 32, "3" * 48
ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "FORBIDDEN_argv_cwd_output_path_hash_digest_task_name_exception"


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.host = Host()

    def tearDown(self):
        # Synthetic jobs have no child handles and must never reach close().
        for manager in (self.host.jobs, self.host._dev_interactive_jobs):
            if manager is not None:
                manager._jobs.clear()
                manager.close()

    def lookup(self, identity=RID):
        return self.host.activity(mode="lookup", id=identity)

    def cached(self, **extra):
        values = dict(request_id=RID, pending_id=PID, session_id=SID,
                      state="RUNNING", source="job", job_state="RUNNING")
        values.update(extra)
        self.host._activity_store.publish(**values)

    def job(self, manager=None, rid=RID, sid=SID, state="COMPLETED"):
        manager = manager or self.host.jobs
        job = SimpleNamespace(request=SimpleNamespace(request_id=rid, argv=[SENTINEL], cwd=SENTINEL),
                              status=state, lock=RLock(), child=Mock(), captured=999,
                              output=SENTINEL)
        manager._jobs[sid] = job
        manager._request_sessions[rid] = sid
        return job

    def review(self):
        command = Command(executable=sys.executable, argv=[SENTINEL], cwd=str(ROOT), request_id=RID)
        pending = self.host.approval.create(command, self.host._context())
        dispatcher = object.__new__(OwnerReviewDispatcher)
        dispatcher._lock = Lock()
        dispatcher._active_pending_id = None
        dispatcher._results = {pending.pending_id: {"status": "RUNNING", "session_id": SID,
                                                    "output": SENTINEL}}
        self.host._owner_review_dispatcher = dispatcher
        return pending.pending_id

    def test_empty_new_chat_and_unknown_are_distinct(self):
        recent = self.host.activity()
        self.assertEqual(recent["status"], "OK")
        self.assertEqual(recent["records"], [])
        self.assertEqual(recent["required_action"], "CHOOSE_LABELLED_ID")
        unknown = self.lookup()
        self.assertEqual(unknown["status"], "UNKNOWN")
        self.assertEqual(unknown["next_tool"], "recovery_overview")
        self.assertEqual(unknown["id_kind"], "unknown")

    def test_command_timeout_is_terminal_and_keeps_store_valid(self):
        pid = self.review()
        dispatcher = self.host._owner_review_dispatcher
        dispatcher._broker = Mock()
        dispatcher._controller = SimpleNamespace(host=self.host)
        for reason in ("CLOSED", "EXPIRED"):
            with self.subTest(reason=reason), patch.object(self.host.jobs, "start") as start:
                dispatcher._broker.review_once.return_value = dict(
                    status="TIMED_OUT", stage="FIRST_GUI", reason=reason,
                    approval_state="EXPIRED", launch_outcome="NOT_STARTED",
                    auto_retry=False, required_action="REPORT_AND_WAIT")
                dispatcher._review(pid)
                result = self.lookup(pid)
                self.assertEqual(result["status"], "OK", result)
                row = result["records"][0]
                self.assertEqual(row["state"], "TIMED_OUT")
                self.assertEqual(row["review_state"], "TIMED_OUT")
                self.assertIsNotNone(row["finished_at"])
                self.assertEqual(row["required_action"], "REPORT_AND_WAIT")
                self.assertIsNone(row["next_tool"])
                self.assertFalse(row["auto_retry"])
                start.assert_not_called()

    def test_r3_stored_launch_is_not_current_job(self):
        pid = self.review()
        job = self.job()
        result = self.lookup(pid)
        row = result["records"][0]
        self.assertEqual((result["id_kind"], row["review_state"], row["job_state"], row["state"]),
                         ("pending_id", "RUNNING", "COMPLETED", "COMPLETED"))
        self.assertEqual(result["next_args"], {"request_id": RID})
        job.child.poll.assert_not_called()

    def test_all_command_identity_kinds(self):
        self.cached()
        self.job(state="RUNNING")
        for identity, kind in ((RID, "request_id"), (PID, "pending_id"), (SID, "session_id")):
            result = self.lookup(identity)
            self.assertEqual(result["id_kind"], kind)
            self.assertEqual(result["count"], 1)

    def test_pending_without_job_routes_pending_not_request(self):
        command = Command(executable=sys.executable, argv=[], cwd=str(ROOT), request_id=RID)
        pending = self.host.approval.create(command, self.host._context())
        result = self.lookup(RID)
        self.assertEqual(result["next_tool"], "review_status")
        self.assertEqual(result["next_args"], {"pending_id": pending.pending_id})

    def test_retired_and_uncertain_ids_after_cache_eviction(self):
        self.host.jobs._retired_request_ids.add(RID)
        self.host.jobs._retired_sessions[RID] = SID
        for identity, kind in ((RID, "request_id"), (SID, "session_id")):
            result = self.lookup(identity)
            self.assertEqual(result["id_kind"], kind)
            row = result["records"][0]
            self.assertEqual(row["state"], "OUTCOME_UNKNOWN")
            self.assertIsNone(row["job_state"])
            self.assertEqual(result["required_action"], "MANUAL_RECONCILIATION")
        self.host.jobs._uncertain_request_ids[PID] = "spawning"
        self.assertEqual(self.lookup(PID)["records"][0]["state_source"], "retired")

    def test_cache_only_expired_source_preserves_kind_without_old_completion(self):
        self.cached(state="COMPLETED", job_state="COMPLETED")
        result = self.lookup()
        self.assertEqual(result["id_kind"], "request_id")
        self.assertEqual(result["records"][0]["state"], "OUTCOME_UNKNOWN")
        self.assertIsNone(result["next_tool"])

    def test_session_without_request_routes_list_sessions(self):
        self.job(rid="legacy")
        result = self.lookup(SID)
        self.assertEqual(result["next_tool"], "list_sessions")
        self.assertIsNone(result["records"][0]["request_id"])
        self.assertNotIn("legacy", json.dumps(result))

    def test_authoritative_membership_without_job_is_never_unknown(self):
        self.host.jobs._request_sessions[RID] = SID
        for identity in (RID, SID):
            result = self.lookup(identity)
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["records"][0]["state"], "OUTCOME_UNKNOWN")
        self.host.jobs._dev_interactive_requests[PID] = object()
        self.assertEqual(self.lookup(PID)["id_kind"], "request_id")
        pending = "4" * 32
        self.host.jobs._stdin_attempts["private_input_id"] = pending
        self.assertEqual(self.lookup(pending)["records"][0]["operation_type"], "send_input")

    def test_namespace_collision_preserves_operations(self):
        self.job()
        self.host.jobs._stdin_pending[RID] = {"result": {"status": "PENDING", "content": SENTINEL}}
        result = self.lookup()
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertEqual({r["operation_type"] for r in result["records"]}, {"command", "send_input"})
        self.assertIsNone(result["next_tool"])

    def test_too_many_matches_returns_no_selected_operation(self):
        for n in range(21):
            rid = f"{n:032x}"
            self.host.jobs._retired_request_ids.add(rid)
            self.host.jobs._retired_sessions[rid] = SID
        result = self.lookup(SID)
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertEqual(result["error"], "TooManyMatches")
        self.assertEqual(result["records"], [])
        self.assertTrue(result["truncated"])

    def test_approval_and_dispatcher_decisions_survive_store_failure(self):
        command = Command(executable=sys.executable, argv=[], cwd=str(ROOT), request_id=RID)
        with patch.object(self.host._activity_store, "publish", side_effect=RuntimeError(SENTINEL)):
            pending = self.host.approval.create(command, self.host._context())
            dispatcher = object.__new__(OwnerReviewDispatcher)
            dispatcher._controller = SimpleNamespace(host=self.host)
            dispatcher._broker = SimpleNamespace(review_once=Mock(return_value={"status": "RUNNING", "session_id": SID}))
            dispatcher._lock, dispatcher._results = Lock(), {}
            dispatcher._review(pending.pending_id)
        self.assertEqual(dispatcher._results[pending.pending_id]["status"], "RUNNING")
        self.assertEqual(self.host.approval.consume_local(pending.pending_id, self.host._context()), pending)
        self.assertEqual(self.host.activity()["error"], "InvalidMetadata")

    def test_same_kind_collision_across_managers(self):
        from sora_commander_next.jobs import JobManager
        self.host._dev_interactive_jobs = JobManager()
        self.cached()
        self.job()
        self.job(self.host._dev_interactive_jobs, sid="4" * 48)
        result = self.lookup()
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertEqual(result["count"], 2)

    def test_input_membership_does_not_wait_or_merge_command(self):
        done = Mock()
        self.host.jobs._stdin_pending[PID] = {"done": done, "result": {"status": "UNKNOWN"}}
        result = self.lookup(PID)
        self.assertEqual(result["id_kind"], "pending_id")
        self.assertEqual(result["records"][0]["operation_type"], "send_input")
        self.assertEqual(result["required_action"], "MANUAL_RECONCILIATION")
        self.assertEqual(result["next_args"], {"pending_id": PID})
        done.wait.assert_not_called()

    def pilot(self):
        from sora_commander_next.edit_review_pilot import EditReviewPilot
        pilot = EditReviewPilot(ROOT, enable_create=True)
        pilot._activity_store = self.host._activity_store
        self.host._edit_review_pilot = pilot
        return pilot

    def test_edit_create_producers_states_eviction_and_routes(self):
        from sora_commander_next import edit_review_pilot as edits
        for create in (False, True):
            for terminal in ('DENIED', 'ERROR', 'CREATED_LOCAL_DEV' if create else 'REPLACED_LOCAL_DEV'):
                with self.subTest(create=create, terminal=terminal):
                    pilot = self.pilot()
                    queued = []
                    thread = Mock()
                    thread.side_effect = lambda **kw: SimpleNamespace(start=lambda: queued.append(kw))
                    preview = dict(diff_truncated=False, changed=True, new_sha256=SENTINEL,
                                   review_truncated=False, path=SENTINEL)
                    with patch.object(pilot, '_target', return_value=SENTINEL), \
                         patch.object(edits, 'preview_text_edit', return_value=preview), \
                         patch.object(edits, 'preview_text_create', return_value=preview), \
                         patch.object(edits, 'Thread', thread):
                        result = (pilot.request_create(SENTINEL, SENTINEL) if create else
                                  pilot.request(SENTINEL, SENTINEL, SENTINEL))
                    pid = result['pending_id']
                    row = self.lookup(pid)['records'][0]
                    self.assertEqual(row['operation_type'], 'text_create' if create else 'text_edit')
                    self.assertEqual(row['state'], 'PENDING')
                    self.assertIsNotNone(row['created_at'])
                    self.assertIsNone(row['finished_at'])
                    self.assertEqual(row['next_tool'], 'edit_review_status')
                    # Exercise the worker's normal terminal publication without disk/UI.
                    import hashlib
                    digest = hashlib.sha256(SENTINEL.encode()).hexdigest()
                    pilot._results[pid]['new_sha256'] = digest
                    outcome = {'status': terminal, 'sha256': digest, 'target': SENTINEL}
                    with patch.object(pilot, '_replace', return_value=outcome), \
                         patch.object(edits, 'preview_text_create', return_value=dict(preview, new_sha256=digest)), \
                         patch.object(edits, 'review_and_create_local', return_value=outcome), \
                         patch.object(edits, '_verify_created_text', return_value=digest):
                        pilot._execute(pid, SENTINEL, SENTINEL, None if create else SENTINEL, digest, create)
                    final = self.lookup(pid)
                    self.assertEqual(final['id_kind'], 'pending_id')
                    self.assertEqual(final['records'][0]['state'], terminal)
                    self.assertIsNotNone(final['records'][0]['finished_at'])
                    self.assertNotIn(SENTINEL, json.dumps(final))
                    self.assertNotIn(digest, json.dumps(final))
                    pilot._results.clear()  # Same observable source loss as 64-result eviction.
                    expired = self.lookup(pid)
                    self.assertEqual(expired['id_kind'], 'pending_id')
                    self.assertEqual(expired['records'][0]['state'], 'OUTCOME_UNKNOWN')
                    self.assertIsNone(expired['next_tool'])
                    del self.host._edit_review_pilot
                    self.assertEqual(self.lookup(pid)['required_action'], 'MANUAL_RECONCILIATION')

    def test_input_producers_each_result_and_guard(self):
        from sora_commander_next import dev_rdc_compat as dev
        from threading import Event
        for state in ('SENT', 'UNKNOWN', 'DENIED', 'START_FAILED', 'TIMED_OUT'):
            for broken in (False, True):
                with self.subTest(state=state, broken=broken):
                    manager = self.host.jobs
                    manager._stdin_pending.clear()
                    manager._stdin_attempts.clear()
                    self.host._activity_store = ActivityStore()
                    manager._activity_store = self.host._activity_store
                    job = SimpleNamespace(stdin_owner=Mock(), request=SimpleNamespace(request_id=RID),
                                          child=Mock(), stdin_uncertain=False, cancel=Event())
                    queued = []
                    def thread(**kw):
                        if state == 'START_FAILED':
                            return SimpleNamespace(start=Mock(side_effect=RuntimeError(SENTINEL)))
                        return SimpleNamespace(start=lambda: queued.append(kw['target']))
                    review = (dev.file_edits._VerifiedFileReviewExpiry(object(), '', None, '', 'interactive_review', reason='CLOSED')
                              if state == 'TIMED_OUT' else None if state == 'DENIED' else float('inf'))
                    with patch.object(dev, '_active_input_job', return_value=job), \
                         patch.object(dev, '_review_typed', return_value=review), \
                         patch.object(dev, 'validate_input', return_value=b'1\n'), \
                         patch('threading.Thread', side_effect=thread), \
                         patch.object(dev.os, 'write', return_value=2 if state == 'SENT' else 1), \
                         patch.object(manager._activity_store, 'publish', wraps=manager._activity_store.publish,
                                      side_effect=RuntimeError(SENTINEL) if broken else None):
                        result = dev.send_input(manager, SID, SENTINEL, RID)
                        self.assertEqual(result['status'], 'PENDING')
                        pid = result['pending_id']
                        if not broken and queued:
                            pending = self.lookup(pid)['records'][0]
                            self.assertEqual(pending['state'], 'PENDING')
                            self.assertEqual(pending['session_id'], SID)
                            self.assertIsNotNone(pending['created_at'])
                        for target in queued:
                            target()
                        expected = 'UNKNOWN' if state == 'START_FAILED' else state
                        self.assertEqual(manager._stdin_pending[pid]['result']['status'], expected)
                    if not broken:
                        final = self.lookup(pid)
                        self.assertEqual(final['records'][0]['state'], expected)
                        if state == 'TIMED_OUT':
                            self.assertEqual(final['records'][0]['required_action'], 'REPORT_AND_WAIT')
                            self.assertIsNone(final['records'][0]['next_tool'])
                            self.assertIsNone(final['records'][0]['job_state'])
                        self.assertIsNotNone(final['records'][0]['finished_at'])
                        self.assertEqual(final['next_tool'], None if state == 'TIMED_OUT' else 'review_status')
                        self.assertNotIn(SENTINEL, json.dumps(final))
                        self.assertEqual(final['records'][0]['operation_type'], 'send_input')

    def test_edit_store_failure_does_not_change_result(self):
        pilot = self.pilot()
        from sora_commander_next import edit_review_pilot as edits
        with patch.object(pilot, '_target', return_value=SENTINEL), \
             patch.object(edits, 'preview_text_edit', return_value=dict(
                 diff_truncated=False, changed=True, new_sha256=SENTINEL)), \
             patch.object(edits, 'Thread') as thread, \
             patch.object(pilot._activity_store, 'publish', side_effect=RuntimeError(SENTINEL)):
            thread.return_value.start.side_effect = RuntimeError('synthetic start failure')
            self.assertEqual(pilot.request(SENTINEL, SENTINEL, SENTINEL),
                             {'status': 'ERROR', 'error': 'ReviewStartFailed'})
        self.assertIsNone(pilot._active)
        self.assertEqual(list(pilot._results.values()), [{'status': 'ERROR', 'error': 'ReviewStartFailed'}])

    def test_edit_create_no_answer_terminal_routes_and_retired_source(self):
        from copy import deepcopy
        for operation in ('text_edit', 'text_create'):
            for reason in ('EXPIRED', 'CLOSED'):
                with self.subTest(operation=operation, reason=reason):
                    self.host._activity_store = ActivityStore()
                    pilot = self.pilot()
                    outcome = dict(status='TIMED_OUT', stage='FILE_GUI', reason=reason,
                                   approval_state='EXPIRED', write_outcome='NOT_STARTED',
                                   auto_retry=False, required_action='REPORT_AND_WAIT', pending_id=PID)
                    with pilot._lock:
                        pilot._results[PID] = outcome
                        pilot._publish_activity(PID, 'PENDING', operation)
                        pilot._publish_activity(PID, 'TIMED_OUT')
                    before = deepcopy(pilot._results)
                    with patch.object(pilot, 'request') as edit, \
                         patch.object(pilot, 'request_create') as create, \
                         patch.object(pilot, 'status') as status:
                        for mode in ('recent', 'lookup'):
                            result = self.host.activity(mode=mode, **({'id': PID} if mode == 'lookup' else {}))
                            self.assertEqual(result['status'], 'OK', result)
                            row = result['records'][0]
                            self.assertEqual(row['operation_type'], operation)
                            self.assertEqual(row['state'], 'TIMED_OUT')
                            self.assertEqual(row['required_action'], 'REPORT_AND_WAIT')
                            self.assertIsNone(row['next_tool'])
                            self.assertEqual(row['next_args'], {})
                            self.assertIsNotNone(row['finished_at'])
                        edit.assert_not_called()
                        create.assert_not_called()
                        status.assert_not_called()
                    self.assertEqual(pilot._results, before)
                    self.assertEqual(pilot.activity_snapshot({PID})[0]['state'], 'TIMED_OUT')
                    pilot._results.clear()
                    retired = self.lookup(PID)
                    self.assertEqual(retired['status'], 'OK')
                    self.assertEqual(retired['records'][0]['state'], 'OUTCOME_UNKNOWN')
                    self.assertEqual(retired['records'][0]['required_action'], 'MANUAL_RECONCILIATION')
                    self.assertIsNone(retired['records'][0]['next_tool'])

    def test_edit_input_command_collision_keeps_all_namespaces(self):
        pilot = self.pilot()
        pilot._results[RID] = {'status': 'DENIED', 'target': SENTINEL}
        pilot._publish_activity(RID, 'DENIED', 'text_create')
        self.job()
        self.host.jobs._stdin_pending[RID] = {'result': {'status': 'SENT'}}
        result = self.lookup()
        self.assertEqual(result['status'], 'AMBIGUOUS')
        self.assertEqual({r['operation_type'] for r in result['records']},
                         {'command', 'send_input', 'text_create'})
        self.assertIsNone(result['next_tool'])

    def test_reads_never_prune_publish_write_or_use_recovery(self):
        self.cached()
        self.job()
        before = deepcopy(self.host._activity_store._rows)
        with patch.object(self.host.jobs, "_prune", side_effect=AssertionError()), \
             patch.object(self.host._activity_store, "publish", side_effect=AssertionError()), \
             patch.object(self.host, "recovery_overview", side_effect=AssertionError()), \
             patch.object(self.host.jobs, "_snapshot_journal", side_effect=AssertionError()):
            for _ in range(3):
                self.assertEqual(self.lookup()["status"], "OK")
                self.assertEqual(self.host.activity()["status"], "OK")
        self.assertEqual(self.host._activity_store._rows, before)

    def test_forbidden_fields_never_cross_projection(self):
        self.cached()
        self.job()
        result = self.lookup()
        self.assertEqual(set(result), REPLY_KEYS)
        self.assertEqual(set(result["records"][0]), RECORD_KEYS)
        self.assertNotIn(SENTINEL, json.dumps(result))
        self.assertNotIn("argv", json.dumps(result))
        self.assertFalse(result["auto_retry"])

    def test_new_source_status_is_invalid_not_a_free_string(self):
        self.job(state=SENTINEL)
        result = self.lookup()
        self.assertEqual(result["error"], "InvalidMetadata")
        self.assertEqual(result["records"], [])
        self.assertNotIn(SENTINEL, json.dumps(result))

    def test_unavailable_source_is_error_not_unknown(self):
        with patch.object(self.host.jobs, "activity_snapshot", side_effect=RuntimeError(SENTINEL)):
            result = self.lookup()
        self.assertEqual(result["error"], "ActivityUnavailable")
        self.assertNotIn(SENTINEL, json.dumps(result))

    def test_store_gap_invalidates_whole_view(self):
        publish(self.host._activity_store, request_id=RID, state=SENTINEL, source="job")
        self.assertEqual(self.host.activity()["error"], "InvalidMetadata")
        self.assertEqual(self.lookup()["error"], "InvalidMetadata")

    def test_store_failure_cannot_change_denied_command_result(self):
        with patch.object(self.host._activity_store, "publish", side_effect=RuntimeError(SENTINEL)):
            result = self.host.run_command(executable=sys.executable, argv=[], cwd=str(ROOT), request_id=RID)
        self.assertEqual(result["status"], "DENY")
        self.assertEqual(self.host.activity()["error"], "InvalidMetadata")

    def test_restart_has_new_scope_window(self):
        self.cached()
        other = Host()
        try:
            self.assertEqual(other.activity()["count"], 0)
            self.assertEqual(other.activity(mode="lookup", id=RID)["status"], "UNKNOWN")
        finally:
            other.close()

    def test_recent_limit_and_stable_admission_order(self):
        for n in range(25):
            self.cached(request_id=f"{n:032x}", pending_id=None, session_id=None)
        result = self.host.activity(limit=20)
        self.assertEqual(result["count"], 20)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["records"][0]["request_id"], f"{24:032x}")
        self.cached(request_id=f"{0:032x}", pending_id=None, session_id=None,
                    state="COMPLETED", job_state="COMPLETED")
        self.assertEqual(self.host.activity(limit=20)["records"][0]["request_id"], f"{24:032x}")

    def test_concurrent_producers_and_snapshots_are_bounded(self):
        def work(n):
            self.cached(request_id=f"{n:032x}", pending_id=None, session_id=None)
            result = self.host.activity()
            self.assertEqual(result["status"], "OK")
            self.assertLessEqual(result["count"], 10)
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(work, range(40)))

    def test_host_strict_arguments_before_source_access(self):
        bad = [{"mode": None}, {"limit": None}, {"limit": True}, {"limit": "10"},
               {"limit": 0}, {"limit": 21}, {"mode": "LOOKUP"}, {"id": RID},
               {"mode": "lookup"}, {"mode": "lookup", "id": RID, "limit": 11},
               {"mode": "lookup", "id": "A" * 32}, {"extra": SENTINEL},
               {"mode": "lookup", "id": " " + RID}]
        with patch.object(self.host._activity_store, "snapshot", side_effect=AssertionError()):
            for values in bad:
                result = self.host.activity(**values)
                self.assertEqual(result["error"], "InvalidArguments", values)
                self.assertIsNone(result["mode"])
                self.assertIsNone(result["query_id"])

    def test_real_sdk_rejects_before_coercion_or_host_call(self):
        server = build_server(self.host)
        with patch.object(self.host, "activity") as call:
            for values in ({"limit": True}, {"limit": "10"}, {"mode": None}, {"limit": None},
                           {"id": RID}, {"bad": SENTINEL}, {"mode": "lookup", "id": []}):
                content, structured = asyncio.run(server.call_tool("activity", values))
                self.assertEqual(structured["error"], "InvalidArguments")
                self.assertEqual(set(structured), REPLY_KEYS)
                self.assertEqual(json.loads(content[0].text), structured)
                self.assertNotIn(SENTINEL, content[0].text)
            call.assert_not_called()
        asyncio.run(server.call_tool("activity", {}))


class StoreTests(unittest.TestCase):
    def test_store_supports_b2_namespaces_without_merging_shared_session(self):
        store = ActivityStore()
        for operation in ("command", "send_input", "text_edit", "text_create"):
            store.publish(operation_type=operation, request_id=RID, pending_id=PID,
                          session_id=SID, state="PENDING", source="activity_only")
        self.assertEqual(len(store.snapshot()), 4)
        self.assertEqual({r["operation_type"] for r in store.snapshot()},
                         {"command", "send_input", "text_edit", "text_create"})

    def test_hard_capacity_ttl_no_read_pruning_and_update_order(self):
        clock = [0.0]
        store = ActivityStore(clock=lambda: clock[0])
        for n in range(513):
            store.publish(request_id=f"{n:032x}", state="RUNNING", job_state="RUNNING", source="job")
        self.assertEqual(len(store._rows), 512)
        self.assertNotIn(("command", "0" * 32), store._rows)
        store.publish(request_id=f"{1:032x}", state="COMPLETED", job_state="COMPLETED", source="job")
        self.assertEqual(store.snapshot()[0]["request_id"], f"{512:032x}")
        self.assertLessEqual(sum(len(json.dumps(r).encode()) for r in store.snapshot()), 1024 * 1024)
        clock[0] = 3600.0
        self.assertEqual(store.snapshot(), [])
        self.assertEqual(len(store._rows), 512)
        store.publish(request_id=RID, state="PENDING", source="review")
        self.assertEqual(len(store._rows), 1)

    def test_times_record_transition_and_finish_without_backfill(self):
        stamps = iter(["2026-11-12T00:00:00Z", "2026-11-12T00:00:01Z", "2026-11-12T00:00:02Z"])
        store = ActivityStore(utc=lambda: next(stamps))
        store.publish(request_id=RID, state="PENDING", source="review")
        store.publish(request_id=RID, session_id=SID, state="RUNNING", source="job", job_state="RUNNING")
        store.publish(request_id=RID, session_id=SID, state="COMPLETED", source="job", job_state="COMPLETED")
        row = store.snapshot()[0]
        self.assertEqual(row["created_at"], "2026-11-12T00:00:00Z")
        self.assertEqual(row["updated_at"], row["finished_at"])
        self.assertEqual(row["finished_at"], "2026-11-12T00:00:02Z")
        self.assertEqual(row["time_basis"], "PRODUCER_EVENT")

    def test_legacy_ids_excluded_and_scalar_signature_only(self):
        store = ActivityStore()
        store.publish(request_id="legacy", state="RUNNING", source="job")
        self.assertEqual(store.snapshot(), [])
        with self.assertRaises(TypeError):
            store.publish(request_id=RID, command=Mock(), state="RUNNING", source="job")

    def test_record_and_whole_reply_validation(self):
        base = Record(request_id=RID, state="RUNNING").data()
        for change in ({"state": SENTINEL}, {"safe_command": SENTINEL}, {"auto_retry": True},
                       {"created_at": "2026-99-12T00:00:00Z"}, {"next_args": {"path": SENTINEL}}):
            with self.assertRaises((InvalidMetadata, TypeError)):
                validate_record(dict(base, **change))
        good = reply("recent", records=[base], action="CHOOSE_LABELLED_ID")
        validate_reply(good, "recent", None)
        for change in ({"count": True}, {"auto_retry": True}, {"extra": SENTINEL},
                       {"records": [base] * 21, "count": 21}, {"error": SENTINEL}):
            with self.assertRaises(InvalidMetadata):
                validate_reply(dict(good, **change), "recent", None)


class WireTests(unittest.TestCase):
    def setUp(self):
        self.execution = Host()
        self.addCleanup(self.execution.close)
        self.service = OwnerBrokerService(OwnerBrokerBackend(self.execution))
        self.transport = SimpleNamespace(request=Mock(side_effect=self.service.handle), close=Mock())
        self.host = DevBrokerHost(self.transport)

    def test_backend_execution_scope_and_single_forward(self):
        self.execution._activity_store.publish(request_id=RID, state="DENY", source="activity_only")
        result = self.host.activity()
        self.assertEqual(result["count"], 1)
        self.transport.request.assert_called_once()
        self.assertEqual(decode_frame(self.transport.request.call_args.args[0])["action"], "activity")

    def test_absent_closed_and_old_broker_have_no_local_fallback(self):
        missing = BrokerHost(UnavailableTransport())
        self.assertEqual(missing.activity()["error"], "ProtectedBrokerUnavailable")
        missing.close()
        self.assertEqual(missing.activity()["error"], "ConnectionClosed")
        self.fake_result({"status": "DENIED", "error": "ActionNotAllowed"})
        self.assertEqual(self.host.activity()["error"], "ActivityUnsupported")

    def fake_result(self, value):
        def respond(raw):
            wire = decode_frame(raw)
            return encode_frame({k: wire[k] for k in ("v", "action", "call_id")} | {"result": value})
        self.transport.request.side_effect = respond

    def test_malformed_unbound_oversize_and_forbidden_replies(self):
        row = Record(request_id=RID, state="RUNNING").data()
        for value in ({"status": "OK"}, reply("lookup", RID),
                      reply("recent", records=[dict(row, output=SENTINEL)]),
                      reply("recent", records=[row] * 21),
                      dict(reply("recent"), auto_retry=True),
                      dict(reply("recent"), next_args={"path": SENTINEL})):
            self.fake_result(value)
            result = self.host.activity()
            self.assertEqual(result["error"], "BrokerReplyUnconfirmed")
            self.assertEqual(result["records"], [])
            self.assertNotIn(SENTINEL, json.dumps(result))
        self.transport.request.side_effect = lambda raw: b"{}"
        self.assertEqual(self.host.activity()["error"], "BrokerReplyUnconfirmed")
        self.fake_result(reply("recent", action="CHOOSE_LABELLED_ID"))
        valid = self.transport.request.side_effect
        self.transport.request.side_effect = lambda raw: b" " * (36 * 1024) + valid(raw)
        self.assertEqual(self.host.activity()["error"], "BrokerReplyUnconfirmed")

    def test_client_invalid_arguments_do_not_touch_wire(self):
        for values in ({"limit": True}, {"mode": None}, {"mode": "lookup", "id": SID, "limit": 1}):
            self.assertEqual(self.host.activity(**values)["error"], "InvalidArguments")
        self.transport.request.assert_not_called()

    def test_wire_validation_precedes_backend_and_preserves_guard(self):
        backend = Mock()
        service = OwnerBrokerService(backend)
        for values in ({}, {"mode": "recent", "extra": SENTINEL},
                       {"mode": "recent", "limit": True}, {"mode": None},
                       {"mode": "lookup", "id": RID, "limit": 11}, {"mode": "recent", "id": RID}):
            wire = encode_frame(dict(v=1, action="activity", call_id=PID, arguments=values))
            self.assertEqual(decode_frame(service.handle(wire))["result"]["error"], "InvalidArguments")
        backend.activity.assert_not_called()
        self.assertEqual(service._seen_calls, set())

    def test_service_validates_whole_backend_reply(self):
        with patch.object(self.execution, "activity", return_value=dict(reply("recent"), output=SENTINEL)):
            self.assertEqual(self.host.activity()["error"], "InvalidMetadata")


if __name__ == "__main__":
    unittest.main()
