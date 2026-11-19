"""Synthetic local reviews and Windows job launches through Host activity."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from sora_commander_next import dev_rdc_compat as dev, jobs, owner_approval_broker
from sora_commander_next.activity import ActivityStore
from sora_commander_next.approval import LocalApproval
from sora_commander_next.host import Host
from sora_commander_next.owner_review_dispatch import OwnerReviewDispatcher
from sora_commander_next.policy import Policy
from sora_commander_next.windows_owner import WindowsOwnerIdentity
from sora_commander_next.windows_owner_controller import WindowsOwnerController
from sora_commander_next.windows_session_events import OwnerSessionEventLatch


RID = "1" * 32


class _FailSessionIndex(dict):
    def __setitem__(self, key, value):
        raise RuntimeError("synthetic session index fault")


class InteractiveActivityTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        root = Path(__file__).resolve().parent
        self.host = Host(Policy((str(root),)), "owner-test", LocalApproval(lambda: self.now))
        self.host._activity_store = self.host.approval._activity_store = ActivityStore(
            clock=lambda: self.now)
        self.addCleanup(self.host.close)
        dev.install_host_interactive(self.host)
        self.manager = self.host._dev_interactive_jobs
        self.payload = dict(executable=sys.executable, argv=("-I", "-S", "-q", "-i"),
                            cwd=str(root), timeout=10, request_id=RID)
        self.guard = Mock(sid="owner-test")
        self.guard._identity = WindowsOwnerIdentity("owner-test", 1, (4, 5), True)
        self.controller = object.__new__(WindowsOwnerController)
        self.controller.host, self.controller._guard = self.host, self.guard
        latch = object.__new__(OwnerSessionEventLatch)
        self.enterContext(patch.object(OwnerSessionEventLatch, "start", return_value=latch))
        self.enterContext(patch.object(OwnerSessionEventLatch, "verify"))
        self.enterContext(patch.object(OwnerSessionEventLatch, "close"))
        self.enterContext(patch.object(dev, "_owner", return_value=self.guard))
        self.enterContext(patch.object(dev, "monotonic", side_effect=lambda: self.now))
        self.enterContext(patch.object(dev.file_edits, "monotonic", side_effect=lambda: self.now))
        self.enterContext(patch("sora_commander_next.windows_owner_controller.monotonic",
                                side_effect=lambda: self.now))
        self.enterContext(patch("sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job"))
        self.gui = self.enterContext(patch.object(dev.file_edits, "_show_edit_review_dev"))
        self.dispatcher = OwnerReviewDispatcher(self.controller)
        self.host.attach_owner_review_local(self.dispatcher)
        self.enterContext(patch.object(self.dispatcher, "offer_local", return_value=True))
        self.enterContext(patch.object(owner_approval_broker, "owner_ui_language", return_value="en"))
        self.enterContext(patch.object(owner_approval_broker, "DIALOG_INTEGRITY"))
        self.enterContext(patch.object(owner_approval_broker, "WindowsJob"))
        dialog = Mock(returncode=0, pid=12345)
        dialog.poll.return_value = 0
        dialog.communicate.side_effect = lambda *, input, timeout: (
            json.dumps({"nonce": json.loads(input)["nonce"], "decision": "APPROVE"}).encode(), b"")
        self.child = Mock()
        self.tree = Mock()
        self.spawn = Mock(side_effect=self.spawn_command)
        self.enterContext(patch.object(jobs.subprocess, "Popen", side_effect=(
            lambda argv, **options: self.spawn(argv, **options) if argv[-1] == "-i" else dialog)))
        self.job_object = self.enterContext(patch.object(jobs, "WindowsJob", side_effect=self.make_job))
        self.enterContext(patch.object(jobs.os, "set_blocking"))
        self.enterContext(patch.object(jobs, "_child_environment", return_value={}))
        self.supervisor = self.enterContext(patch.object(jobs, "Thread"))

    def make_job(self):
        self.assertNotIn(self.request_id, self.manager._dev_interactive_requests)
        self.assertEqual(self.manager._uncertain_request_ids[self.request_id], "pre_spawn")
        return self.tree

    def spawn_command(self, argv, **options):
        self.assertNotIn(self.request_id, self.manager._dev_interactive_requests)
        self.assertEqual(options["creationflags"], 0x00000004)
        return self.child

    def submit(self, request_id=RID):
        self.request_id = request_id
        submission = self.host.run_command(background=True, interactive=True,
                                           **dict(self.payload, request_id=request_id))
        self.assertEqual(submission["status"], "REVIEW", submission)
        return submission["pending_id"]

    def review(self, pending_id, decision, *, inspect=None):
        def window(*args, deadline, _review_token, **kwargs):
            if inspect is not None:
                inspect()
            if decision == "EXPIRED":
                self.now = deadline
            timer = self.now if decision in ("EXPIRED", "CLOSED") else None
            return dev.file_edits._DevFileReviewOutcome(
                decision, "LOCAL_EVENT", deadline, timer, _review_token)
        self.gui.side_effect = window
        self.dispatcher._review(pending_id)
        return self.host.await_command_result(pending_id, wait_seconds=0)

    def assert_activity(self, state, source, action="REPORT_AND_WAIT", tool=None):
        for mode in ("recent", "lookup"):
            reply = self.host.activity(mode=mode, id=self.request_id if mode == "lookup" else None)
            self.assertEqual(reply["status"], "OK", reply)
            rows = [row for row in reply["records"] if row["request_id"] == self.request_id]
            self.assertEqual(len(rows), 1, reply)
            row = rows[0]
            self.assertEqual((row["state"], row["state_source"]), (state, source))
            self.assertEqual(row["job_state"], state if source == "job" else None)
            self.assertEqual((row["required_action"], row["next_tool"]), (action, tool))
            if mode == "lookup":
                self.assertEqual((reply["required_action"], reply["next_tool"]), (action, tool))

    def assert_no_answer(self, reason):
        pending = self.submit()
        ticket = self.manager._dev_interactive_requests[RID]
        result = self.review(pending, reason, inspect=lambda: self.assert_activity(
            "PENDING", "review", "OBSERVE_EXISTING", "review_status"))
        self.assertEqual(result["status"], "TIMED_OUT", result)
        self.assertEqual((result["reason"], result["stage"], result["launch_outcome"]),
                         (reason, "SECOND_GUI", "NOT_STARTED"))
        self.assertIs(self.manager._dev_interactive_requests[RID], ticket)
        self.assert_activity("TIMED_OUT", "review")
        self.assertEqual(self.submit(), pending)
        self.assertEqual(self.dispatcher._broker.review_once(pending)["status"], "ERROR")
        self.gui.assert_called_once()
        self.spawn.assert_not_called()
        self.job_object.assert_not_called()

    def test_expired_open_and_replay(self):
        self.assert_no_answer("EXPIRED")

    def test_closed_open_and_replay(self):
        self.assert_no_answer("CLOSED")

    def test_rejected(self):
        result = self.review(self.submit(), "REJECT")
        self.assertEqual((result["status"], result["error"], result["stage"]),
                         ("ERROR", "OwnerAuthorizationRequired", "SECOND_GUI"))
        self.assert_activity("ERROR", "review")
        self.spawn.assert_not_called()

    def test_failure_after_gui_consent_before_start(self):
        pending = self.submit()
        ticket = self.manager._dev_interactive_requests[RID]

        def extend_ticket():
            ticket["expires"] += 1

        result = self.review(pending, "APPROVED", inspect=extend_ticket)
        self.assertEqual((result["status"], result["error"], result["stage"]),
                         ("ERROR", "OwnerAuthorizationRequired", "REVALIDATE"))
        self.assert_activity("ERROR", "review")
        self.spawn.assert_not_called()

    def test_unmarked_ticket_keeps_unknown_fallback(self):
        self.submit()
        self.assert_activity("OUTCOME_UNKNOWN", "retired",
                             "MANUAL_RECONCILIATION", "request_status")

    def test_replacement_ticket_does_not_inherit_review_end(self):
        pending = self.submit()
        ticket = self.manager._dev_interactive_requests[RID]
        replacement = {**ticket, "authorized": False}

        def replace_ticket():
            self.manager._dev_interactive_requests[RID] = replacement

        self.assertEqual(self.review(pending, "CLOSED", inspect=replace_ticket)["status"],
                         "TIMED_OUT")
        self.assertIs(self.manager._dev_interactive_requests[RID], replacement)
        self.assert_activity("OUTCOME_UNKNOWN", "retired",
                             "MANUAL_RECONCILIATION", "request_status")
        self.spawn.assert_not_called()

    def test_unknown_fingerprint_takes_precedence_over_review_end(self):
        pending = self.submit()
        request = self.manager._dev_interactive_requests[RID]["request"]

        def record_uncertain_shape():
            self.manager._unknown_command_fingerprints.add(jobs._unknown_command_fingerprint(request))

        self.assertEqual(self.review(pending, "CLOSED", inspect=record_uncertain_shape)["status"],
                         "TIMED_OUT")
        self.assert_activity("OUTCOME_UNKNOWN", "retired",
                             "MANUAL_RECONCILIATION", "request_status")

    def assert_uncertain_launch(self, phase):
        result = self.review(self.submit(), "APPROVED")
        self.assertEqual((result["status"], result["launch_phase"]), ("OUTCOME_UNKNOWN", phase))
        self.tree.attach_and_resume.assert_called_once_with(self.child)
        self.assertEqual(self.manager._uncertain_request_ids[RID], phase)
        self.assert_activity("OUTCOME_UNKNOWN", "retired",
                             "MANUAL_RECONCILIATION", "request_status")

    def test_resume_failure_keeps_uncertain_entry(self):
        self.tree.attach_and_resume.side_effect = RuntimeError("synthetic resume fault")
        self.assert_uncertain_launch("resuming")

    def test_supervisor_start_failure_keeps_uncertain_entry(self):
        self.supervisor.return_value.start.side_effect = RuntimeError("synthetic supervisor fault")
        self.assert_uncertain_launch("resumed")

    def test_session_index_failure_keeps_uncertain_entry(self):
        self.manager._request_sessions = _FailSessionIndex()
        self.assert_uncertain_launch("supervised")

    def test_uncertain_original_takes_precedence_over_new_timed_out_ticket(self):
        self.supervisor.return_value.start.side_effect = RuntimeError("synthetic post-resume fault")
        result = self.review(self.submit(), "APPROVED")
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN", result)
        self.now += 3601
        result = self.review(self.submit(), "CLOSED")
        self.assertEqual(result["status"], "TIMED_OUT", result)
        self.assert_activity("OUTCOME_UNKNOWN", "retired",
                             "MANUAL_RECONCILIATION", "request_status")
        self.spawn.assert_called_once()

    def test_suspended_rollback_then_closed_review_proves_no_execution(self):
        self.supervisor.side_effect = RuntimeError("synthetic pre-resume fault")
        pending = self.submit()
        result = self.review(pending, "APPROVED")
        self.assertEqual((result["error"], result["launch_phase"], result["launch_outcome"]),
                         ("LaunchNotStarted", "spawned_suspended", "NOT_STARTED"))
        self.tree.attach_and_resume.assert_not_called()
        self.assertNotIn(RID, self.manager._request_sessions)
        self.assertNotIn(RID, self.manager._retired_request_ids)
        self.assertNotIn(RID, self.manager._uncertain_request_ids)
        self.now += 3601
        new_pending = self.submit()
        self.assertNotEqual(pending, new_pending)
        self.assertEqual(self.review(new_pending, "CLOSED")["status"], "TIMED_OUT")
        self.assert_activity("TIMED_OUT", "review")
        self.spawn.assert_called_once()
