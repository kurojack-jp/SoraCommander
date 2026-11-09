"""Offline faults: two explicit decisions, no real GUI, Host or process launch."""
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import sys
from threading import Event, RLock, Thread, current_thread
import unittest
from unittest.mock import Mock, patch

from sora_commander_next import dev_rdc_compat as dev
from sora_commander_next.approval import LocalApproval, diagnostic_fields
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.jobs import JobManager, LaunchNotStarted, LaunchOutcomeUnknown
from sora_commander_next.owner_review_dispatch import OwnerReviewDispatcher
from sora_commander_next.policy import Decision, Policy
from sora_commander_next.windows_owner import WindowsOwnerIdentity
from sora_commander_next.windows_owner_controller import WindowsOwnerController
from sora_commander_next.windows_session_events import OwnerSessionEventLatch


class SecondGuiSafetyTests(unittest.TestCase):
    def setUp(self):
        self.now = self.approval_now = 100.0
        self.root = str(Path(__file__).resolve().parent)
        self.host = Host(Policy((self.root,)), 'owner-test',
                         LocalApproval(lambda: self.approval_now))
        self.host.jobs.protected_stage = True
        self.addCleanup(self.host.close)
        dev.install_host_interactive(self.host)
        self.manager = self.host._dev_interactive_jobs
        self.guard = Mock(sid='owner-test')
        self.guard._identity = WindowsOwnerIdentity('owner-test', 1, (4, 5), True)
        self.controller = object.__new__(WindowsOwnerController)
        self.controller.host, self.controller._guard = self.host, self.guard
        self.invalidated = False
        self.latch = object.__new__(OwnerSessionEventLatch)
        def verify_latch(_latch):
            if self.invalidated:
                raise PermissionError('SECRET session detail')
        self.enterContext(patch.object(OwnerSessionEventLatch, 'verify', verify_latch))
        self.enterContext(patch.object(dev, '_owner', return_value=self.guard))
        self.enterContext(patch.object(dev, 'monotonic', side_effect=lambda: self.now))
        self.enterContext(patch('sora_commander_next.windows_owner_controller.monotonic',
                                side_effect=lambda: self.now))
        self.enterContext(patch('sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job'))
        self.actual_dialog = dev.file_edits._show_edit_review
        self.gui = self.enterContext(patch.object(dev.file_edits, '_show_edit_review', return_value=True))
        self.request = Command(sys.executable, ('-I', '-S', '-q', '-i'), self.root,
                               timeout=10, interactive=True)
        self.host._owner_review_dispatcher = Mock()
        dev.prepare_interactive(self.host, self.request, True, Decision.REVIEW)
        self.pending = self.host.approval.create(self.request, self.host._context())
        def start(request):
            self.assertIs(dev.approve_interactive_launch(self.manager, request), self.guard)
            return {'status': 'RUNNING', 'session_id': 'a' * 48}
        self.launch = self.enterContext(patch.object(self.manager, 'start', side_effect=start))

    def approve(self):
        return self.controller.approve_local(self.pending.pending_id, session_latch=self.latch)

    def monitor_mutation(self, mutate):
        completed, failures = Event(), []
        policy, normal, manager = self.host.policy, self.host.jobs, self.manager
        def worker():
            try:
                with policy.lock, normal._lock, manager._lock:
                    mutate()
                    completed.set()
            except BaseException as exc:
                failures.append(exc)
        thread = Thread(target=worker, daemon=True)
        def monitor(_request):
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'monitor retained a required lock')
        try:
            with patch('sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job',
                       side_effect=monitor):
                result = self.approve()
        finally:
            if thread.ident is not None:
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(completed.is_set())
        self.assert_stopped(result)
        self.gui.assert_not_called()

    def test_monitor_generation_change_prevents_gui2(self):
        self.monitor_mutation(lambda: self.host.policy.revoke_owner_grants_local('owner-test'))

    def test_monitor_approval_expiry_prevents_gui2(self):
        self.monitor_mutation(lambda: setattr(self, 'approval_now', self.pending.expires_at))

    def test_monitor_deadline_prevents_gui2(self):
        self.monitor_mutation(lambda: setattr(self, 'now', 160.0))

    def test_monitor_does_not_change_second_gui_budget(self):
        def monitor(_request):
            self.now = self.approval_now = 110.0
        def gui(*args, **kwargs):
            self.assertEqual(kwargs['deadline'], 155.0)
            return True
        self.gui.side_effect = gui
        with patch('sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job',
                   side_effect=monitor):
            self.assertEqual(self.approve()['status'], 'RUNNING')
        self.gui.assert_called_once()
        self.launch.assert_called_once()

    def test_monitor_owner_change_prevents_gui2(self):
        self.monitor_mutation(lambda: setattr(self.host, 'owner', 'another-owner'))

    def test_monitor_identity_change_prevents_gui2(self):
        self.monitor_mutation(lambda: setattr(self.guard, '_identity',
            replace(self.guard._identity, logon_luid=(6, 7))))

    def test_monitor_session_history_prevents_gui2(self):
        self.monitor_mutation(lambda: setattr(self, 'invalidated', True))

    def test_monitor_classification_change_prevents_gui2(self):
        self.monitor_mutation(lambda: self.enterContext(patch.object(
            self.host.policy, 'classify', return_value=Decision.DENY)))

    def test_monitor_approval_replacement_prevents_gui2(self):
        self.monitor_mutation(lambda: setattr(self.host, 'approval', LocalApproval()))

    def test_monitor_policy_replacement_prevents_gui2(self):
        original = self.host.policy
        replacement = Policy(original.roots)
        replacement.instance_id = original.instance_id
        self.monitor_mutation(lambda: setattr(self.host, 'policy', replacement))

    def test_monitor_binding_drift_prevents_gui2(self):
        self.monitor_mutation(lambda: object.__setattr__(
            self.request, 'argv', ('synthetic-binding-drift',)))

    def test_monitor_manager_close_prevents_gui2(self):
        self.monitor_mutation(self.manager.close)

    def test_monitor_manager_replacement_prevents_gui2(self):
        replacement = JobManager()
        dev.install_interactive_hooks(replacement)
        self.addCleanup(self.manager.close)
        self.monitor_mutation(lambda: setattr(self.host, '_dev_interactive_jobs', replacement))

    def test_monitor_hooks_replacement_prevents_gui2(self):
        self.monitor_mutation(lambda: dev.install_interactive_hooks(self.manager))

    def test_monitor_ticket_replacement_prevents_gui2(self):
        records = self.manager._dev_interactive_requests
        self.monitor_mutation(lambda: records.__setitem__(
            self.request.request_id, dict(records[self.request.request_id])))

    def test_monitor_ticket_expiry_change_prevents_gui2(self):
        self.monitor_mutation(lambda: self.manager._dev_interactive_requests[
            self.request.request_id].__setitem__('expires', 999.0))

    def test_monitor_ticket_expiry_prevents_gui2(self):
        self.manager._dev_interactive_requests[self.request.request_id]['expires'] = 110.0
        self.monitor_mutation(lambda: setattr(self, 'now', 110.0))

    def test_monitor_cwd_identity_drift_prevents_gui2(self):
        self.monitor_mutation(lambda: self.enterContext(patch.object(
            dev, '_interactive_launch_shape', return_value=('synthetic-replacement',))))

    def test_monitor_shape_validation_expiry_prevents_gui2(self):
        shape = dev._interactive_launch_shape
        def expire(request, roots):
            identity = shape(request, roots)
            self.approval_now = self.pending.expires_at
            return identity
        self.monitor_mutation(lambda: self.enterContext(patch.object(
            dev, '_interactive_launch_shape', side_effect=expire)))

    def test_monitor_shape_validation_ticket_expiry_prevents_gui2(self):
        self.manager._dev_interactive_requests[self.request.request_id]['expires'] = 110.0
        shape = dev._interactive_launch_shape
        def expire(request, roots):
            identity = shape(request, roots)
            self.now = 110.0
            return identity
        self.monitor_mutation(lambda: self.enterContext(patch.object(
            dev, '_interactive_launch_shape', side_effect=expire)))

    def test_monitor_unlocks_all_locks_and_preserves_second_gui(self):
        acquired = []
        def worker():
            for lock in (self.host.policy.lock, self.host.jobs._lock, self.manager._lock):
                ok = lock.acquire(blocking=False)
                acquired.append(ok)
                if ok:
                    lock.release()
        def monitor(_request):
            thread = Thread(target=worker, daemon=True)
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        with patch('sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job',
                   side_effect=monitor):
            result = self.approve()
        self.assertEqual(acquired, [True, True, True])
        self.assertEqual(result['status'], 'RUNNING', result)
        self.gui.assert_called_once()
        self.launch.assert_called_once()

    def assert_stopped(self, result):
        self.assertEqual(result['status'], 'ERROR', result)
        self.assertEqual(result['launch_outcome'], 'NOT_STARTED', result)
        self.launch.assert_not_called()
        self.assertFalse(self.host.approval.pending_local())
        self.assertNotIn('SECRET', repr(result))
        self.assertEqual(self.approve()['status'], 'ERROR')

    def test_gui_releases_both_locks_and_consumed_binding_is_frozen(self):
        def gui(*args, **kwargs):
            acquired = []
            def probe():
                for lock in (self.host.policy.lock, self.manager._lock):
                    ok = lock.acquire(blocking=False)
                    acquired.append(ok)
                    if ok:
                        lock.release()
            thread = Thread(target=probe)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(acquired, [True, True])
            self.assertFalse(self.host.approval.pending_local())
            with self.assertRaises(FrozenInstanceError):
                self.pending.expires_at = 999
            self.assertIn('interactive Python', kwargs['title'])
            self.assertIn('Second approval', kwargs['wording'])
            self.assertNotIn('file edit', kwargs['wording'])
            return True
        self.gui.side_effect = gui
        result = self.approve()
        self.assertEqual(result['launch_outcome'], 'STARTED')
        self.assertEqual(result['approval_state'], 'SECOND_APPROVED')
        self.assertTrue(self.host.jobs.protected_stage)
        self.launch.assert_called_once()
        self.assertEqual(self.approve()['status'], 'ERROR')
        self.gui.assert_called_once()
        with self.assertRaises(PermissionError):
            dev.approve_interactive_launch(self.manager, self.request)

    def test_gui2_ticket_replacement_fails_closed(self):
        records = self.manager._dev_interactive_requests
        def gui(*args, **kwargs):
            records[self.request.request_id] = dict(records[self.request.request_id])
            return True
        self.gui.side_effect = gui
        self.assert_stopped(self.approve())

    def test_gui2_ticket_expiry_extension_fails_closed(self):
        records = self.manager._dev_interactive_requests
        def gui(*args, **kwargs):
            records[self.request.request_id]['expires'] += 100
            return True
        self.gui.side_effect = gui
        self.assert_stopped(self.approve())

    def test_missing_first_approval_never_opens_second_gui(self):
        self.host.approval.reject_local(self.pending.pending_id)
        self.assert_stopped(self.approve())
        self.gui.assert_not_called()

    def test_revalidation_matches_normal_then_dev_lock_order(self):
        main_thread = current_thread()
        waiting, held = Event(), Event()
        normal_lock = RLock()
        observed = []
        class ObservedLock:
            def __enter__(self):
                if current_thread() is main_thread and held.is_set():
                    waiting.set()
                normal_lock.acquire()
            def __exit__(self, *args):
                normal_lock.release()
        self.host.jobs._lock = ObservedLock()
        def observer():
            with self.host.jobs._lock:
                held.set()
                if waiting.wait(2):
                    acquired = self.manager._lock.acquire(blocking=False)
                    observed.append(acquired)
                    if acquired:
                        self.manager._lock.release()
        worker = Thread(target=observer)
        def gui(*args, **kwargs):
            worker.start()
            self.assertTrue(held.wait(2))
            return True
        self.gui.side_effect = gui
        try:
            result = self.approve()
        finally:
            waiting.set()
            worker.join(3)
            self.host.jobs._lock = normal_lock
        self.assertFalse(worker.is_alive())
        self.assertEqual(observed, [True])
        self.assertEqual(result['status'], 'RUNNING', result)

    def test_session_history_is_required(self):
        self.assert_stopped(self.controller.approve_local(self.pending.pending_id))
        self.gui.assert_not_called()

    def test_second_denial_spends_first_approval(self):
        self.gui.return_value = False
        self.assert_stopped(self.approve())
        self.gui.assert_called_once()

    def test_second_gui_exception_does_not_leak(self):
        self.gui.side_effect = RuntimeError('SECRET argv cwd exception')
        self.assert_stopped(self.approve())

    def fault(self, mutate):
        self.gui.side_effect = lambda *a, **kw: (mutate(), True)[1]
        self.assert_stopped(self.approve())

    def test_policy_generation_changes(self):
        self.fault(lambda: setattr(self.host.policy, 'generation', 1))

    def test_policy_roots_change(self):
        self.fault(lambda: setattr(self.host.policy, 'roots', ()))

    def test_policy_identity_changes(self):
        self.fault(lambda: setattr(self.host.policy, 'instance_id', 'replacement'))

    def test_equal_context_policy_object_replacement(self):
        original = self.host.policy
        replacement = Policy(original.roots)
        replacement.instance_id = original.instance_id
        self.fault(lambda: setattr(self.host, 'policy', replacement))

    def test_owner_sid_changes(self):
        self.fault(lambda: setattr(self.host, 'owner', 'another-owner'))

    def test_owner_luid_changes(self):
        self.fault(lambda: setattr(self.guard, '_identity',
                   replace(self.guard._identity, logon_luid=(6, 7))))

    def test_wts_lock_then_unlock_history_stays_invalid(self):
        self.fault(lambda: setattr(self, 'invalidated', True))

    def test_unlock_state_unknown(self):
        self.fault(lambda: setattr(self.guard.verify, 'side_effect', PermissionError('SECRET unlock')))

    def test_manager_closed_during_gui(self):
        self.fault(self.manager.close)

    def test_dev_manager_replaced(self):
        replacement = JobManager()
        dev.install_interactive_hooks(replacement)
        self.addCleanup(self.manager.close)
        self.fault(lambda: setattr(self.host, '_dev_interactive_jobs', replacement))

    def test_dev_hooks_replaced(self):
        self.fault(lambda: dev.install_interactive_hooks(self.manager))

    def test_approval_object_replaced(self):
        self.fault(lambda: setattr(self.host, 'approval', LocalApproval()))

    def test_original_approval_expiry_during_gui(self):
        self.fault(lambda: setattr(self, 'approval_now', self.pending.expires_at))

    def test_ticket_expiry_during_gui(self):
        self.fault(lambda: setattr(self, 'now', 160.0))

    def test_second_gui_budget_is_minimum_without_extension(self):
        # Each boundary is tested at equality, with distinct original clocks.
        self.host.approval.reject_local(self.pending.pending_id)
        for approval_remaining, ticket_remaining, expected in ((60, 60, 45), (12, 60, 12), (60, 7, 7)):
            with self.subTest(expected=expected):
                # Restore the original immutable pending only as a new fixture.
                request = replace(self.request, request_id=__import__('uuid').uuid4().hex)
                self.request = request
                dev.prepare_interactive(self.host, request, True, Decision.REVIEW)
                self.pending = self.host.approval.create(request, self.host._context())
                self.approval_now = self.pending.expires_at - approval_remaining
                self.manager._dev_interactive_requests[request.request_id]['expires'] = self.now + ticket_remaining
                def gui(*a, **kw):
                    self.assertEqual(kw['deadline'] - self.now, expected)
                    self.now = kw['deadline']
                    return True
                self.gui.side_effect = gui
                self.assert_stopped(self.approve())

    def test_original_deadline_checked_after_final_shape_validation(self):
        original = dev._interactive_launch_shape
        entered_launch = False
        start = self.launch.side_effect
        def launch(request):
            nonlocal entered_launch
            entered_launch = True
            return start(request)
        self.launch.side_effect = launch
        def delayed(*args):
            result = original(*args)
            if entered_launch:  # ticket consume, after start() entry
                self.approval_now = self.pending.expires_at
            return result
        with patch.object(dev, '_interactive_launch_shape', side_effect=delayed):
            result = self.approve()
        # A generic permission failure after start() entry has no proven outcome.
        self.assertEqual(result['launch_outcome'], 'UNKNOWN')
        self.assertEqual(result['stage'], 'LAUNCH')
        self.assertNotIn(self.request.request_id, self.manager._dev_interactive_requests)

    def wire_broker(self, decision='APPROVE'):
        dispatcher = OwnerReviewDispatcher(self.controller)
        self.host.attach_owner_review_local(dispatcher)
        child = Mock(returncode=0, pid=12345)
        child.poll.return_value = 0
        child.stdin.closed = child.stdout.closed = True
        child.communicate.side_effect = lambda *, input, timeout: (
            json.dumps({'nonce': json.loads(input)['nonce'], 'decision': decision}).encode('ascii'), b'')
        self.enterContext(patch('sora_commander_next.owner_approval_broker.subprocess.Popen', return_value=child))
        self.enterContext(patch('sora_commander_next.owner_approval_broker.WindowsJob'))
        self.enterContext(patch.object(OwnerSessionEventLatch, 'start', return_value=self.latch))
        self.enterContext(patch.object(OwnerSessionEventLatch, 'close'))
        return dispatcher

    def test_first_gui_denial_does_not_open_second(self):
        dispatcher = self.wire_broker('DENY')
        dispatcher._review(self.pending.pending_id)
        result = self.host.await_command_result(self.pending.pending_id, wait_seconds=0)
        self.assertEqual(result['status'], 'DENIED')
        self.assertEqual(result['approval_state'], 'REJECTED')
        self.gui.assert_not_called()
        self.launch.assert_not_called()

    def test_diagnostics_survive_entire_chain_and_unknown_never_retries(self):
        dispatcher = self.wire_broker()
        self.launch.side_effect = LaunchOutcomeUnknown(self.request.request_id, 'resumed', 'SECRET argv cwd')
        dispatcher._review(self.pending.pending_id)
        review = self.host.review_status(self.pending.pending_id)
        awaited = self.host.await_command_result(self.pending.pending_id, wait_seconds=0)
        self.assertEqual(review['status'], 'OUTCOME_UNKNOWN')
        self.assertEqual(review['launch_outcome'], 'UNKNOWN')
        self.assertEqual(review['launch_phase'], 'resumed')
        self.assertEqual(review['approval_state'], 'SECOND_APPROVED')
        self.assertEqual({k: awaited[k] for k in review}, review)
        self.assertEqual(dispatcher.result_local()['reason'], 'LAUNCH_UNKNOWN')
        self.assertNotIn('SECRET', repr(awaited))
        self.assertTrue(dispatcher.offer_local(self.pending.pending_id))
        self.launch.assert_called_once()

    def test_controller_error_preserved_by_broker_dispatcher_await(self):
        dispatcher = self.wire_broker()
        self.gui.return_value = False
        dispatcher._review(self.pending.pending_id)
        result = self.host.await_command_result(self.pending.pending_id, wait_seconds=0)
        self.assertEqual(result['error'], 'OwnerAuthorizationRequired')
        self.assertEqual(result['stage'], 'SECOND_GUI')
        self.assertEqual(result['approval_state'], 'CONSUMED')
        self.assertEqual(result['launch_outcome'], 'NOT_STARTED')

    def test_permission_error_from_start_is_unknown(self):
        dispatcher = self.wire_broker()
        self.launch.side_effect = PermissionError('SECRET unexpected spawn boundary')
        dispatcher._review(self.pending.pending_id)
        result = self.host.await_command_result(self.pending.pending_id, wait_seconds=0)
        self.assertEqual(result['status'], 'OUTCOME_UNKNOWN')
        self.assertEqual(result['error'], 'LaunchOutcomeUnknown')
        self.assertEqual(result['stage'], 'LAUNCH')
        self.assertEqual(result['approval_state'], 'SECOND_APPROVED')
        self.assertEqual(result['launch_outcome'], 'UNKNOWN')
        self.assertNotIn('SECRET', repr(result))
        self.launch.assert_called_once()
        self.assertTrue(dispatcher.offer_local(self.pending.pending_id))
        self.launch.assert_called_once()
    def test_launch_not_started_preserved(self):
        dispatcher = self.wire_broker()
        self.launch.side_effect = LaunchNotStarted(self.request.request_id, 'spawned_suspended', 'SECRET')
        dispatcher._review(self.pending.pending_id)
        result = self.host.await_command_result(self.pending.pending_id, wait_seconds=0)
        self.assertEqual(result['error'], 'LaunchNotStarted')
        self.assertEqual(result['stage'], 'LAUNCH')
        self.assertEqual(result['launch_outcome'], 'NOT_STARTED')

    def test_unexpected_dispatch_exception_remains_unknown(self):
        dispatcher = self.wire_broker()
        with patch.object(self.controller, 'approve_local', side_effect=RuntimeError('SECRET')):
            dispatcher._review(self.pending.pending_id)
        result = dispatcher.review_status(self.pending.pending_id)
        self.assertEqual(result['status'], 'OUTCOME_UNKNOWN')
        self.assertEqual(result['approval_state'], 'UNKNOWN')
        self.assertNotIn('SECRET', repr(result))

    def test_malformed_launch_response_is_unknown(self):
        self.launch.side_effect = None
        for value in (None, {}, {'status': ['SECRET']}, {'status': 'SECRET'}):
            self.launch.return_value = value
            # _launch is tested directly to avoid replaying an approval.
            result = self.controller._launch(self.pending, 'CONSUMED')
            self.assertEqual(result['status'], 'OUTCOME_UNKNOWN')
            self.assertEqual(result['launch_outcome'], 'UNKNOWN')
            self.assertNotIn('SECRET', repr(result))

    def test_diagnostic_projection_rejects_arbitrary_values_and_types(self):
        for value in ('SECRET argv cwd', [], {}, None):
            self.assertEqual(diagnostic_fields({'stage': value, 'reason': value,
                'approval_state': value, 'launch_outcome': value, 'error': value,
                'launch_phase': value, 'argv': ['SECRET']}), {})

    def test_malformed_broker_result_is_terminal_unknown(self):
        dispatcher = self.wire_broker()
        for value in (None, {}, {'status': ['SECRET']}, {'status': 'SECRET'}):
            with patch.object(dispatcher._broker, 'review_once', return_value=value):
                dispatcher._review(self.pending.pending_id)
            result = dispatcher.review_status(self.pending.pending_id)
            self.assertEqual(result['status'], 'OUTCOME_UNKNOWN')
            self.assertEqual(result['launch_outcome'], 'UNKNOWN')
            self.assertNotIn('SECRET', repr(result))

    def test_actual_dialog_session_poll_denies_and_uses_original_timer(self):
        from sora_commander_next import file_edits
        fake_tk = Mock()
        root = fake_tk.Tk.return_value
        root.winfo_screenwidth.return_value = 1920
        root.winfo_screenheight.return_value = 1080
        timers = []
        root.after.side_effect = lambda delay, callback: timers.append((delay, callback))
        root.mainloop.side_effect = lambda: next(callback for delay, callback in timers if delay == 0)()
        verify = Mock(side_effect=PermissionError('SECRET session unavailable'))
        with patch.dict(sys.modules, {'tkinter': fake_tk}), \
                patch.object(file_edits, 'monotonic', return_value=100):
            result = self.actual_dialog({'path': 'DEV interactive Python', 'diff': ''},
                                        deadline=107, verify=verify)
        self.assertFalse(result)
        self.assertEqual(timers[0][0], 7000)
        verify.assert_called_once()
        root.quit.assert_called_once()
        root.destroy.assert_called_once()


if __name__ == '__main__':
    unittest.main()
