"""Focused DEV stdin fixtures: mocked Owner GUI, no real Owner acceptance."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event
from time import monotonic, sleep
from uuid import uuid4
import unittest
from unittest.mock import Mock, patch

from sora_commander_next import dev_rdc_compat as dev
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.jobs import JobManager, _Job
from sora_commander_next.mcp_adapter import _validate_arguments
from sora_commander_next.policy import Decision, Policy


class StdinTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(__file__).resolve().parents[1]
        self.root = self.workspace / 'tests' / ('rdc-step2-temp-' + uuid4().hex)
        self.root.mkdir()
        def cleanup():
            if self.root.parent != self.workspace / 'tests' or not self.root.name.startswith('rdc-step2-temp-'):
                raise RuntimeError('unsafe fixture cleanup')
            shutil.rmtree(self.root)
        self.addCleanup(cleanup)
        self.manager = JobManager()
        self.addCleanup(self.manager.close)
        # Same registration the MCP adapter's DEV block performs; release-side
        # code (Host, JobManager, Owner controller) only sees the hook slot.
        dev.install_interactive_hooks(self.manager)
        self.host = Host(Policy(roots=(str(self.root),)), owner='test-owner')
        self.addCleanup(self.host.close)
        self.host._dev_interactive_jobs = self.manager
        self.host._owner_review_dispatcher = Mock()
        self.guard = Mock(sid='test-owner')
        self.enterContext(patch.object(dev, '_owner', return_value=self.guard))
        self.gui = self.enterContext(patch.object(dev.file_edits, '_show_edit_review', return_value=True))
        def typed_window(*args, **kwargs):
            decision = self.gui(*args, **kwargs)
            if type(decision) is not bool:
                return decision
            return dev.file_edits._DevFileReviewOutcome(
                'APPROVED' if decision else 'REJECT', 'LOCAL_EVENT',
                kwargs['deadline'], None, kwargs['_review_token'])
        self.enterContext(patch.object(dev.file_edits, '_show_edit_review_dev', side_effect=typed_window))

    def command(self, **kw):
        values = dict(executable=sys.executable, argv=('-I', '-S', '-q', '-i'),
                      cwd=str(self.root), timeout=10, interactive=True)
        values.update(kw)
        return Command(**values)

    def fake_job(self, **kw):
        request = self.command(**kw)
        child = Mock()
        child.poll.return_value = None
        child.stdin.fileno.return_value = 123
        job = _Job('a' * 48, request, child, monotonic() + 10, stdin_owner=self.guard)
        job.thread = Mock()
        self.manager._jobs[job.session_id] = job
        return job

    def submit(self, sid='a' * 48, text='print(1+2)\n', iid=None):
        return dev.send_input(self.manager, sid, text, iid or uuid4().hex)

    def observe(self, result):
        return self.host.await_command_result(result['pending_id'], wait_seconds=2)

    def test_wire_and_payloads_fail_closed(self):
        for value in (b'1\n', '\ud800\n', '1\x00\n', '1\t\n', '1\r\n', '1\x7f\n',
                      '1\u202e\n', '1\n2\n', '1'*1025+'\n', "print('secret')\n",
                      '__import__("os").environ\n', 'open("private")\n', '2**999\n'):
            with self.subTest(value=repr(value)), self.assertRaises((ValueError, UnicodeError, SyntaxError)):
                _validate_arguments('send_input', dict(session_id='a'*48, input_text=value, input_id='b'*32))
        for iid in ('a', 'A'*32, 1, 'g'*32):
            self.assertEqual(self.submit(iid=iid)['status'], 'DENIED')
        for flag in (1, 'true', None):
            with self.assertRaises(ValueError):
                self.command(interactive=flag)

    def test_one_write_exact_escaped_gui_and_no_input_in_result(self):
        job = self.fake_job()
        iid = uuid4().hex
        with patch.object(dev.os, 'write', return_value=11) as write:
            result = self.submit(iid=iid)
            final = self.observe(result)
            self.assertEqual(final['status'], 'SENT')
            write.assert_called_once_with(123, b'print(1+2)\n')
            self.assertEqual(self.submit(iid=iid)['status'], 'DENIED')
            self.assertEqual(self.submit(text='print(5)\n', iid=iid)['status'], 'DENIED')
        review = self.gui.call_args.kwargs['complete_review']
        self.assertIn('print(1+2)\\n', review)
        self.assertNotIn('print(1+2)\n', review)
        self.assertNotIn('print(1+2)', json.dumps(final))
        self.assertEqual(self.host.review_status(result['pending_id']), final)
        self.manager._jobs.clear()

    def test_error_and_short_write_never_replay_even_with_new_id(self):
        for error in (OSError('private exception'), BlockingIOError(), 2):
            job = self.fake_job()
            iid = uuid4().hex
            with patch.object(dev.os, 'write') as write:
                if isinstance(error, Exception):
                    write.side_effect = error
                else:
                    write.return_value = error
                result = self.observe(self.submit(iid=iid))
                self.assertEqual(result['status'], 'UNKNOWN')
                self.assertEqual(self.submit(iid=iid)['status'], 'DENIED')
                self.assertEqual(self.submit()['status'], 'DENIED')
                write.assert_called_once()
                self.assertTrue(job.cancel.is_set())
            self.manager._jobs.clear()

    def test_rpc_wait_timeout_only_observes_same_input(self):
        self.fake_job()
        release = Event()
        entered = Event()
        def review(*args, **kwargs):
            entered.set()
            release.wait(2)
            return True
        self.gui.side_effect = review
        iid = uuid4().hex
        with patch.object(dev.os, 'write', return_value=11) as write:
            result = self.submit(iid=iid)
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.host.await_command_result(result['pending_id'], wait_seconds=0)['status'], 'PENDING')
            self.assertEqual(self.submit(iid=iid)['status'], 'DENIED')
            release.set()
            self.assertEqual(self.observe(result)['status'], 'SENT')
            write.assert_called_once()
        self.manager._jobs.clear()

    def test_foreign_missing_owner_noninteractive_and_finished_denied(self):
        with patch.object(dev.os, 'write') as write:
            self.assertEqual(self.submit(sid='f'*48)['status'], 'DENIED')
            job = self.fake_job(interactive=False)
            self.assertEqual(self.submit()['status'], 'DENIED')
            job.request = self.command()
            self.guard.verify.side_effect = PermissionError('private identity')
            self.assertEqual(self.submit()['status'], 'DENIED')
            self.guard.verify.side_effect = None
            for terminal in ('done', 'cancel', 'expired', 'stderr', 'pruned'):
                job = self.fake_job()
                if terminal == 'done': job.done.set()
                if terminal == 'cancel': job.cancel.set()
                if terminal == 'expired': job.deadline = monotonic()-1
                if terminal == 'stderr': job.chunks = [(0, 'stderr', b'private stderr')]
                if terminal == 'pruned': self.manager._jobs.clear()
                self.assertEqual(self.submit()['status'], 'DENIED')
            write.assert_not_called()
        self.manager._jobs.clear()

    def test_denied_gui_and_owner_loss_consumes_id(self):
        self.fake_job()
        iid = uuid4().hex
        self.gui.return_value = False
        with patch.object(dev.os, 'write') as write:
            self.assertEqual(self.observe(self.submit(iid=iid))['status'], 'DENIED')
            self.gui.return_value = True
            self.assertEqual(self.submit(iid=iid)['status'], 'DENIED')
            def lose_owner(*args, **kwargs):
                self.guard.verify.side_effect = PermissionError()
                return True
            self.gui.side_effect = lose_owner
            self.assertEqual(self.observe(self.submit())['status'], 'ERROR')
            write.assert_not_called()
        self.manager._jobs.clear()

    def test_stop_close_while_gui_pending_never_writes_or_deadlocks(self):
        self.fake_job()
        release, entered = Event(), Event()
        def review(*args, **kwargs):
            entered.set()
            release.wait(2)
            return True
        self.gui.side_effect = review
        with patch.object(dev.os, 'write') as write:
            result = self.submit()
            self.assertTrue(entered.wait(1))
            started = monotonic()
            self.manager.stop('a'*48)
            self.manager.close()
            self.assertLess(monotonic()-started, .5)
            release.set()
            self.assertEqual(self.observe(result)['status'], 'ERROR')
            write.assert_not_called()
        self.manager._jobs.clear()

    def test_launch_allowlist_review_and_one_shot_ticket(self):
        request = self.command()
        for changes in (dict(argv=('-i',)), dict(argv=('-i', 'private.py')),
                        dict(executable=str(self.root/'cmd.exe')), dict(timeout=121)):
            with self.assertRaises(PermissionError):
                dev.prepare_interactive(self.host, self.command(**changes), True, Decision.REVIEW)
        for decision in (Decision.AUTO, Decision.SCOPED, Decision.DENY):
            with self.assertRaises(PermissionError):
                dev.prepare_interactive(self.host, request, True, decision)
        with self.assertRaises(PermissionError):
            dev.prepare_interactive(self.host, request, False, Decision.REVIEW)
        dev.prepare_interactive(self.host, request, True, Decision.REVIEW)
        with self.assertRaises(PermissionError):
            self.manager.start(request)
        # The previous attempt consumed its unapproved ticket; no spawn.
        dev.prepare_interactive(self.host, request, True, Decision.REVIEW)
        dev.authorize_interactive_local(self.host, request)
        self.assertIs(dev.approve_interactive_launch(self.manager, request), self.guard)
        with self.assertRaises(PermissionError):
            dev.approve_interactive_launch(self.manager, request)

    def test_noninteractive_popen_remains_devnull(self):
        request = self.command(interactive=False)
        with patch('sora_commander_next.jobs.subprocess.Popen', side_effect=OSError()) as spawn:
            with self.assertRaises(RuntimeError):
                self.manager.start(request)
            self.assertEqual(spawn.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_existing_owner_controller_consumes_review_before_interactive_ticket(self):
        from sora_commander_next.windows_owner_controller import WindowsOwnerController
        from sora_commander_next.windows_session_events import OwnerSessionEventLatch
        request = self.command()
        dev.prepare_interactive(self.host, request, True, Decision.REVIEW)
        pending = self.host.approval.create(request, self.host._context())
        controller = object.__new__(WindowsOwnerController)
        controller.host, controller._guard = self.host, self.guard
        def start(command):
            self.assertFalse(self.host.approval.matches(
                pending.pending_id, request, self.host._context()))
            self.assertIs(dev.approve_interactive_launch(self.manager, command), self.guard)
            return {'status': 'RUNNING', 'session_id': 'a'*48}
        with patch('sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job'), \
                patch.object(self.manager, 'start', side_effect=start) as launch, \
                patch.object(OwnerSessionEventLatch, 'verify', return_value=True):
            latch = object.__new__(OwnerSessionEventLatch)
            self.assertEqual(controller.approve_local(pending.pending_id, session_latch=latch)['status'], 'RUNNING')
            self.assertEqual(controller.approve_local(pending.pending_id)['status'], 'ERROR')
            launch.assert_called_once_with(request)
        self.gui.assert_called_once()

    def test_changed_request_and_host_admission_never_spawn(self):
        request = self.command()
        dev.prepare_interactive(self.host, request, True, Decision.REVIEW)
        with self.assertRaises(PermissionError):
            dev.prepare_interactive(self.host,
                self.command(request_id=request.request_id, timeout=11), True, Decision.REVIEW)
        pending = self.host.approval.create(request, self.host._context())
        with self.assertRaises(PermissionError):
            self.host.approval.create(self.command(request_id=request.request_id, interactive=False),
                                      self.host._context())
        with patch.object(self.manager, 'start') as launch:
            result = self.host.run_command(executable=request.executable,
                argv=list(request.argv), cwd=request.cwd, timeout=10,
                interactive=True, background=False)
            self.assertEqual(result['status'], 'ERROR')
            launch.assert_not_called()

    def test_python_numeric_process_output_paging_and_stop_close(self):
        # Actual isolated Python + existing Windows Job Object, synthetic GUI
        # and Owner identity. This is NOT real Owner/desktop acceptance.
        request = self.command()
        dev.prepare_interactive(self.host, request, True, Decision.REVIEW)
        dev.authorize_interactive_local(self.host, request)
        launched = self.manager.start(request)
        sid = launched['session_id']
        self.assertIsNotNone(self.manager._jobs[sid].tree)
        result = self.submit(sid=sid)
        self.assertEqual(self.observe(result)['status'], 'SENT')
        deadline = monotonic() + 3
        while monotonic() < deadline:
            output = self.host.read_process_output(sid)
            if '3' in output['stdout']:
                break
            sleep(.01)
        self.assertEqual(output['stdout'].strip(), '3')
        self.assertNotIn('print(1+2)', output['stdout']+output['stderr'])
        page = self.host.read_process_output(sid, offset=output['next_offset'])
        self.assertEqual(page['stdout'], '')
        self.assertEqual(page['stderr'], '')
        self.assertEqual(self.host.await_process_output(sid, wait_seconds=0)['session_id'], sid)
        self.host.stop_process(sid)
        finished = self.manager.wait_for(sid, 2)
        self.assertEqual(finished['status'], 'CANCELLED')
        self.assertTrue(self.manager._jobs[sid].child.stdin.closed)
        started = monotonic()
        self.manager.close()
        self.assertLess(monotonic()-started, 1.5)
        self.assertEqual(self.submit(sid=sid)['status'], 'DENIED')

    def test_interactive_denied_decision_is_deny_not_error(self):
        # cwd outside every configured root classifies as DENY. An interactive
        # request must surface that plain denial (P3-3), never the DEV
        # precondition ERROR, and must not record a review or approval.
        result = self.host.run_command(
            executable=sys.executable, argv=['-I', '-S', '-q', '-i'],
            cwd=str(self.root.parent), timeout=10,
            interactive=True, background=True, request_id='d' * 32)
        self.assertEqual(result, {'request_id': 'd' * 32, 'status': 'DENY'})
        self.assertEqual(self.manager._dev_interactive_requests, {})
        self.assertIsNone(
            self.host.approval.pending_id_for_request_local('d' * 32))

    def test_interactive_precondition_failure_has_stable_label(self):
        # REVIEW decision but a launch-shape precondition fails (argv is not
        # the exact allowlist): the result must carry the diagnosable label
        # InteractivePreconditionFailed instead of a bare PermissionError.
        result = self.host.run_command(
            executable=sys.executable, argv=['-i'], cwd=str(self.root),
            timeout=10, interactive=True, background=True)
        self.assertEqual(result['status'], 'ERROR')
        self.assertEqual(result['error'], 'InteractivePreconditionFailed')
        self.assertEqual(self.manager._dev_interactive_requests, {})

    def test_archive_skips_interactive_and_input_guard_capacity(self):
        job = self.fake_job()
        self.manager.output_archive_path = self.root/'must-not-exist'
        with patch('sora_commander_next.offline_result_archive.write_result_checkpoint') as checkpoint:
            self.manager._snapshot_result_archive(job)
            checkpoint.assert_not_called()
        self.manager._stdin_attempts = {str(i): None for i in range(1024)}
        self.assertEqual(self.submit()['status'], 'DENIED')
        self.manager._jobs.clear()

    def test_design_a_protected_normal_manager_stays_separate(self):
        from sora_commander_next.mcp_adapter import build_server
        normal = JobManager(journal_path=self.root/'journal.json', protected_stage=True)
        self.host.jobs = normal
        original = self.manager
        build_server(self.host)
        self.assertIs(self.host._dev_interactive_jobs, original)
        request = self.command()
        result = self.host.run_command(executable=request.executable, argv=list(request.argv),
            cwd=request.cwd, timeout=request.timeout, request_id=request.request_id,
            interactive=True, background=True)
        self.assertEqual(result['status'], 'REVIEW')
        self.assertTrue(normal.protected_stage)
        self.assertIsNone(self.manager.journal_path)
        self.assertIsNone(self.manager.output_archive_path)
        self.assertIn(request.request_id, self.manager._dev_interactive_requests)
        self.assertEqual(normal._dev_interactive_requests, {})
        with patch('sora_commander_next.jobs.subprocess.Popen') as spawn:
            with self.assertRaises(PermissionError):
                normal.start(request)
            spawn.assert_not_called()
        self.assertFalse((self.root/'journal.json').exists())

    def test_design_a_dedicated_manager_denies_normal_commands(self):
        from sora_commander_next.mcp_adapter import build_server
        host = Host()
        self.addCleanup(host.close)
        build_server(host)
        dev_jobs = host._dev_interactive_jobs
        self.assertIsNot(dev_jobs, host.jobs)
        with patch('sora_commander_next.jobs.subprocess.Popen') as spawn:
            with self.assertRaises(PermissionError):
                dev_jobs.start(self.command(interactive=False))
            spawn.assert_not_called()
        build_server(host)
        self.assertIs(host._dev_interactive_jobs, dev_jobs)

    def test_design_a_routes_output_status_stop_and_marks_nonrecoverable(self):
        job = self.fake_job()
        self.manager._request_sessions[job.request.request_id] = job.session_id
        with patch.object(self.host.jobs, 'read', side_effect=AssertionError('normal read')):
            self.assertEqual(self.host.read_process_output(job.session_id)['session_id'], job.session_id)
        self.assertEqual(self.host.request_status(job.request.request_id)['session_id'], job.session_id)
        self.assertEqual(self.host.list_sessions()['count'], 1)
        overview = self.host.recovery_overview()
        self.assertFalse(overview['sessions'][0]['recoverable'])
        self.assertEqual(overview['sessions'][0]['persistence'], 'NONE_DEV_INTERACTIVE')
        self.assertFalse(overview['dev_interactive']['auto_retry'])
        self.assertEqual(self.host.stop_process(job.session_id)['status'], 'CANCELLING')
        self.manager._jobs.clear()

    def test_design_a_unknown_restart_ids_never_route_or_submit(self):
        from sora_commander_next.mcp_adapter import build_server
        host = Host()
        self.addCleanup(host.close)
        server = build_server(host)
        with patch.object(host.jobs, 'read') as read, patch.object(host.jobs, 'stop') as stop:
            self.assertEqual(host.read_process_output('a'*48)['status'], 'ERROR')
            self.assertEqual(host.await_process_output('a'*48, wait_seconds=0)['status'], 'ERROR')
            self.assertEqual(host.stop_process('a'*48)['status'], 'ERROR')
            read.assert_not_called()
            stop.assert_not_called()
        self.assertEqual(host.request_status('a'*32)['status'], 'UNKNOWN')
        self.assertEqual(host.await_command_result('b'*32, wait_seconds=0)['status'], 'DENY')
        self.assertFalse(host.recovery_overview()['dev_interactive']['recoverable'])
        import asyncio
        _, result = asyncio.run(server.call_tool('send_input', dict(
            session_id='a'*48, input_text='print(1+2)\n', input_id='b'*32)))
        self.assertEqual(result['status'], 'DENIED')
        self.assertEqual(host._dev_interactive_jobs._stdin_pending, {})

    def test_design_a_cross_manager_request_and_session_conflicts_fail_closed(self):
        request = self.command()
        dev.prepare_interactive(self.host, request, True, Decision.REVIEW)
        with self.assertRaises(PermissionError):
            self.host._manager_for_request(self.command(request_id=request.request_id, interactive=False))
        self.host.jobs._retired_request_ids.add('c'*32)
        with self.assertRaises(PermissionError):
            self.host._manager_for_request(self.command(request_id='c'*32))
        job = self.fake_job()
        self.host.jobs._jobs[job.session_id] = job
        with self.assertRaises(PermissionError):
            self.host._manager_for_session(job.session_id)
        self.host.jobs._jobs.clear()
        self.manager._jobs.clear()

    def test_design_a_persistent_dev_manager_rejected_before_gui(self):
        for attribute, value in (('journal_path', self.root/'journal'),
                                 ('output_archive_path', self.root/'archive'),
                                 ('protected_stage', True)):
            with self.subTest(attribute=attribute):
                old = getattr(self.manager, attribute)
                setattr(self.manager, attribute, value)
                with self.assertRaises(PermissionError):
                    dev.prepare_interactive(self.host, self.command(), True, Decision.REVIEW)
                setattr(self.manager, attribute, old)
        self.gui.assert_not_called()

    def test_design_a_protected_host_numeric_sdk_roundtrip(self):
        import asyncio
        from sora_commander_next.mcp_adapter import build_server
        from sora_commander_next.windows_owner_controller import WindowsOwnerController
        from sora_commander_next.windows_session_events import OwnerSessionEventLatch
        self.host.jobs = JobManager(journal_path=self.root/'journal.json',
            output_archive_path=self.root/'owner-output-archive.json', protected_stage=True)
        sdk = build_server(self.host)
        request = self.command()
        pending = self.host.run_command(executable=request.executable, argv=list(request.argv),
            cwd=request.cwd, timeout=10, request_id=request.request_id,
            interactive=True, background=True)
        self.assertEqual(pending['status'], 'REVIEW')
        controller = object.__new__(WindowsOwnerController)
        controller.host, controller._guard = self.host, self.guard
        with patch('sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job'), \
                patch.object(OwnerSessionEventLatch, 'verify', return_value=True):
            latch = object.__new__(OwnerSessionEventLatch)
            launched = controller.approve_local(pending['pending_id'], session_latch=latch)
        self.assertEqual(launched['status'], 'RUNNING', launched)
        sid = launched['session_id']
        _, submitted = asyncio.run(sdk.call_tool('send_input', dict(
            session_id=sid, input_text='print(1+2)\n', input_id=uuid4().hex)))
        self.assertEqual(self.observe(submitted)['status'], 'SENT')
        deadline = monotonic()+2
        while monotonic() < deadline:
            output = self.host.read_process_output(sid)
            if '3' in output.get('stdout', ''):
                break
            sleep(.01)
        self.assertIn('3', output['stdout'])
        self.assertTrue(self.host.jobs.protected_stage)
        self.assertFalse((self.root/'journal.json').exists())
        self.assertFalse((self.root/'owner-output-archive.json').exists())
        self.host.stop_process(sid)
        self.assertEqual(self.host.await_process_output(sid, wait_seconds=2)['status'], 'CANCELLED')

    def test_design_a_closed_host_cannot_install_fresh_manager(self):
        host = Host()
        host.close()
        with self.assertRaises(PermissionError):
            dev.install_host_interactive(host)
        self.assertIsNone(host._dev_interactive_jobs)

    def test_design_a_close_closes_both_managers(self):
        with patch.object(self.host.jobs, 'close') as normal, patch.object(self.manager, 'close') as interactive:
            self.host.close()
        normal.assert_called_once()
        interactive.assert_called_once()


class InteractiveHookWiringTests(unittest.TestCase):
    """Dependency inversion: release-side code denies interactive requests
    when no DEV hooks are installed, and dispatches through installed hooks
    without ever importing the DEV module itself."""

    def setUp(self):
        from tempfile import TemporaryDirectory
        self.temp = TemporaryDirectory(prefix="scn-hook-wiring-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.manager = JobManager()
        self.addCleanup(self.manager.close)
        self.host = Host(Policy(roots=(str(self.root),)),
                         owner='test-owner', jobs=self.manager)

    def submit(self):
        return self.host.run_command(
            executable=sys.executable, argv=['-I', '-S', '-q', '-i'],
            cwd=str(self.root), timeout=10, interactive=True, background=True)

    def test_absent_hooks_deny_interactive_review(self):
        result = self.submit()
        self.assertEqual(result['status'], 'DENY')
        self.assertEqual(result['error'], 'DevInteractiveUnavailable')
        self.assertEqual(self.manager._dev_interactive_requests, {})

    def test_installed_hooks_receive_review_prepare(self):
        from sora_commander_next.interactive_hooks import (
            InteractiveHooks, install_hooks)
        calls = []
        def fake_prepare(host, request, background, decision):
            calls.append((host, request, background, decision))
        self.host._dev_interactive_jobs = JobManager()
        self.addCleanup(self.host.close)
        install_hooks(self.host._dev_interactive_jobs, InteractiveHooks(
            prepare_interactive=fake_prepare,
            approve_interactive_launch=lambda manager, request: None,
            authorize_interactive_local=lambda host, request: None,
            input_status=lambda manager, pending_id, wait_seconds=0: {}))
        result = self.submit()
        self.assertEqual(result['status'], 'REVIEW')
        self.assertIn('pending_id', result)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0], self.host)
        self.assertEqual(calls[0][2], True)
        self.assertEqual(calls[0][3], Decision.REVIEW)


if __name__ == '__main__':
    unittest.main()
