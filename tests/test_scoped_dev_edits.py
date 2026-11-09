"""Isolated offline policy/CAS tests; synthetic Owner and ACL, never live GUI/IPC."""
from dataclasses import FrozenInstanceError
from hashlib import sha256
import json
import os
import subprocess
from pathlib import Path
import shutil
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from sora_commander_next import file_edits as edits
from sora_commander_next import scoped_dev_edits as scoped
from sora_commander_next.file_reads import ReadBoundaryError


class ScopedEditTests(unittest.TestCase):
    def setUp(self):
        # Python 3.13 TemporaryDirectory uses mkdir(0700) on Windows, assigning
        # a special DACL. This task forbids ACL changes; inherit a normal DACL.
        self.folder = scoped._REPO / 'tests' / ('scoped-inherited-' + uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.target = self.folder / 'development.py'
        self.target.write_bytes(b'before\r\n')
        self.path = str(self.target)
        self.digest = sha256(self.target.read_bytes()).hexdigest()
        self.policy = scoped.ScopedDevelopmentEditPolicy((self.path,))
        self.adapter = scoped.ScopedDevelopmentEditAdapter(self.policy)
        self.guard = SimpleNamespace(sid='S-1-5-21-1', verify=Mock(return_value=True))
        self.now = 100.0
        for obj, name, kwargs in (
            (scoped, '_owner_guard', {'return_value': self.guard}),
            (scoped, '_acl_snapshot', {'return_value': ('owner-inherited-acl',)}),
            (scoped, 'monotonic', {'side_effect': lambda: self.now}),
            (edits, '_require_inherited_windows_acl', {'return_value': None}),
            (edits, '_show_edit_review', {'return_value': True}),
        ):
            handle = patch.object(obj, name, **kwargs)
            mock = handle.start()
            self.addCleanup(handle.stop)
            if name == '_show_edit_review':
                self.ui = mock
        self.addCleanup(setattr, scoped, '_MANUAL_REVIEW', False)

    def request(self, **overrides):
        return self.adapter.request(**({'path': self.path, 'content': 'after\n',
                                       'expected_sha256': self.digest} | overrides))

    def test_exact_file_approved_and_postverified_with_existing_writer(self):
        sibling = self.folder / 'other.py'
        sibling.write_bytes(b'keep')
        with patch.object(edits, 'replace_text', wraps=edits.replace_text) as writer:
            result = self.request()
        self.assertEqual(result, {'status': 'REPLACED_LOCAL_DEV',
                                  'sha256': sha256(b'after\n').hexdigest()})
        writer.assert_called_once()
        self.assertEqual(self.target.read_bytes(), b'after\n')
        self.assertEqual(sibling.read_bytes(), b'keep')
        self.assertEqual(list(self.folder.glob('.sora-next-*')), [])
        review = json.loads(self.ui.call_args.kwargs['complete_review'])
        self.assertEqual(review['before_utf8'], 'before\r\n')
        self.assertEqual(review['after_utf8'], 'after\n')
        self.assertEqual(review['old_sha256'], self.digest)
        self.assertEqual(review['new_sha256'], result['sha256'])
        self.assertEqual(review['path'], self.path)

    def test_default_is_empty_and_configuration_is_immutable(self):
        empty = scoped.ScopedDevelopmentEditAdapter(scoped.ScopedDevelopmentEditPolicy())
        self.assertEqual(empty.request(self.path, 'after', self.digest)['status'], 'DENIED')
        self.ui.assert_not_called()
        with self.assertRaises(FrozenInstanceError):
            self.policy.files = ()
        for config in ((str(self.folder),), (self.path, self.path), [self.path]):
            with self.subTest(config=config), self.assertRaises((ReadBoundaryError, TypeError)):
                scoped.ScopedDevelopmentEditPolicy(config)
        with self.assertRaises(TypeError):
            self.request(roots=(str(self.folder),))

    def test_disallowed_paths_and_aliases_never_write(self):
        other = self.folder / 'other.py'
        other.write_bytes(b'before\r\n')
        for path in (str(other), str(self.folder), 'development.py',
                     str(self.folder / '..' / self.folder.name / 'development.py'),
                     self.path + ':stream', str(scoped._REPO.parent / 'SoraCommander' / 'x.py'),
                     str(self.folder / '*.py'), str(self.folder / '.secret'),
                     r'\\server\share\x.py', self.path + '.', None):
            with self.subTest(path=path), patch.object(edits, 'replace_text') as write:
                self.assertEqual(self.request(path=path)['status'], 'DENIED')
                write.assert_not_called()
        self.ui.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'before\r\n')

    def test_denied_approval_preserves_file_and_does_not_stage(self):
        self.ui.return_value = False
        with patch.object(edits, 'replace_text') as write:
            self.assertEqual(self.request(), {'status': 'DENIED'})
            write.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'before\r\n')

    def test_expiry_during_gui_does_not_write(self):
        def approve(*args, **kwargs):
            self.now += 45
            return True
        self.ui.side_effect = approve
        with patch.object(edits, 'replace_text') as write:
            self.assertEqual(self.request(), {'status': 'DENIED', 'error': 'APPROVAL_EXPIRED'})
            write.assert_not_called()

    def test_expiry_during_staging_does_not_commit(self):
        original = edits.os.fsync
        def expire(fd):
            original(fd)
            self.now += 46
        with patch.object(edits.os, 'fsync', side_effect=expire), patch.object(edits.os, 'replace') as rename:
            self.assertEqual(self.request()['error'], 'APPROVAL_EXPIRED')
            rename.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'before\r\n')
        self.assertEqual(list(self.folder.glob('.sora-next-*')), [])

    def test_bad_digests_and_content_types_do_not_show_gui(self):
        for digest in ('0' * 64, '', None, True, 'A' * 64, '0' * 65):
            self.assertEqual(self.request(expected_sha256=digest)['status'], 'DENIED')
        for content in (None, b'bytes', True, '\ud800', 'a' * 65537):
            self.assertEqual(self.request(content=content)['status'], 'DENIED')
        self.ui.assert_not_called()

    def test_truncated_or_substituted_preview_never_prompts(self):
        original = edits.preview_text_edit
        for field, bad in (('diff_truncated', True), ('new_sha256', '0' * 64),
                           ('old_sha256', '0' * 64), ('path', 'elsewhere'), ('changed', False)):
            def preview(*args, **kwargs):
                return original(*args, **kwargs) | {field: bad}
            with self.subTest(field=field), patch.object(edits, 'preview_text_edit', side_effect=preview):
                self.assertEqual(self.request()['status'], 'DENIED')
        self.ui.assert_not_called()

    def test_entire_review_must_fit_no_truncation(self):
        self.assertEqual(self.request(content='\x01' * 6000)['status'], 'DENIED')
        self.ui.assert_not_called()

    def test_unchanged_does_not_prompt_or_write(self):
        with patch.object(edits, 'replace_text') as write:
            self.assertEqual(self.request(content='before\r\n')['status'], 'UNCHANGED_LOCAL_DEV')
            write.assert_not_called()
        self.ui.assert_not_called()

    def test_approval_cannot_be_reused(self):
        self.assertEqual(self.request()['status'], 'REPLACED_LOCAL_DEV')
        self.ui.return_value = False
        self.assertEqual(self.request(content='second', expected_sha256=sha256(b'after\n').hexdigest()),
                         {'status': 'DENIED'})
        self.assertEqual(self.ui.call_count, 2)
        self.assertEqual(self.target.read_bytes(), b'after\n')

    def test_content_change_while_owner_reviews_is_preserved(self):
        def approve(*args, **kwargs):
            self.target.write_bytes(b'external change')
            return True
        self.ui.side_effect = approve
        self.assertEqual(self.request()['status'], 'DENIED')
        self.assertEqual(self.target.read_bytes(), b'external change')

    def test_same_content_at_replaced_identity_is_denied(self):
        def approve(*args, **kwargs):
            swapped = self.folder / 'replacement'
            swapped.write_bytes(b'before\r\n')
            swapped.replace(self.target)
            return True
        self.ui.side_effect = approve
        self.assertEqual(self.request()['status'], 'DENIED')
        self.assertEqual(self.target.read_bytes(), b'before\r\n')

    def test_abnormal_or_changed_acl_is_denied(self):
        with patch.object(scoped, '_acl_snapshot', side_effect=ReadBoundaryError('ACL unavailable')):
            self.assertEqual(self.request()['status'], 'DENIED')
        self.ui.assert_not_called()
        def approve(*args, **kwargs):
            scoped._acl_snapshot.return_value = ('changed-acl',)
            return True
        self.ui.side_effect = approve
        self.assertEqual(self.request()['status'], 'DENIED')

    def test_owner_session_change_after_approval_denies(self):
        def approve(*args, **kwargs):
            self.guard.verify.side_effect = PermissionError('locked')
            return True
        self.ui.side_effect = approve
        self.assertEqual(self.request()['status'], 'DENIED')
        self.assertEqual(self.target.read_bytes(), b'before\r\n')

    def test_reparse_symlink_and_junction_attributes_fail_closed(self):
        original = Path.lstat
        for linked in (self.target, self.folder, scoped._REPO):
            def lstat(path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if path == linked:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info
            with self.subTest(linked=linked), patch.object(Path, 'lstat', lstat):
                self.assertEqual(self.request()['status'], 'DENIED')
                with self.assertRaises(ReadBoundaryError):
                    scoped.ScopedDevelopmentEditPolicy((self.path,))
        self.ui.assert_not_called()

    def test_hardlink_is_denied(self):
        linked = self.folder / 'linked.py'
        os.link(self.target, linked)
        self.assertEqual(self.request()['status'], 'DENIED')
        with self.assertRaises(ReadBoundaryError):
            scoped.ScopedDevelopmentEditPolicy((self.path,))
        self.ui.assert_not_called()

    def test_real_symlink_if_available_is_denied(self):
        linked = self.folder / 'symlink.py'
        try:
            linked.symlink_to(self.target)
        except OSError as exc:
            self.skipTest(f'unprivileged symlink unavailable: {type(exc).__name__}')
        with self.assertRaises(ReadBoundaryError):
            scoped.ScopedDevelopmentEditPolicy((str(linked),))

    def test_staged_content_substitution_does_not_commit(self):
        original = edits.os.fsync
        def substitute(fd):
            original(fd)
            # Windows stage still open: modify through its own descriptor.
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b'WRONG\n')
        with patch.object(edits.os, 'fsync', side_effect=substitute), patch.object(edits.os, 'replace') as rename:
            self.assertEqual(self.request()['status'], 'DENIED')
            rename.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'before\r\n')

    def test_concurrent_pending_edits_across_instances_are_busy(self):
        entered, release = Event(), Event()
        def approve(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError()
            return False
        self.ui.side_effect = approve
        worker = Thread(target=self.request)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            second = scoped.ScopedDevelopmentEditAdapter(self.policy)
            self.assertEqual(second.request(self.path, 'other', self.digest), {'status': 'BUSY'})
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_rename_error_is_unknown_and_latches_without_retry_or_rollback(self):
        with patch.object(edits.os, 'replace', side_effect=OSError('ambiguous')):
            self.assertEqual(self.request(), scoped.ScopedDevelopmentEditAdapter._unknown())
        self.assertEqual(self.target.read_bytes(), b'before\r\n')
        with patch.object(edits, 'replace_text') as write:
            self.assertEqual(self.request()['status'], 'UNKNOWN')
            write.assert_not_called()
        self.assertEqual(self.ui.call_count, 1)

    def test_commit_then_exception_is_unknown_without_restore(self):
        original = edits.os.replace
        def commit_then_error(*args):
            original(*args)
            raise OSError('lost completion')
        with patch.object(edits.os, 'replace', side_effect=commit_then_error):
            self.assertEqual(self.request()['status'], 'UNKNOWN')
        self.assertEqual(self.target.read_bytes(), b'after\n')

    def test_corrupt_postwrite_digest_requires_manual_review(self):
        original = edits.os.replace
        def corrupt(*args):
            original(*args)
            self.target.write_bytes(b'corrupt')
        with patch.object(edits.os, 'replace', side_effect=corrupt):
            self.assertEqual(self.request()['recovery'], 'MANUAL_REVIEW_REQUIRED')
        self.assertEqual(self.target.read_bytes(), b'corrupt')

    def test_commit_interruption_is_unknown(self):
        original = edits.os.replace
        def interrupted(*args):
            original(*args)
            raise KeyboardInterrupt()
        with patch.object(edits.os, 'replace', side_effect=interrupted):
            self.assertEqual(self.request(), scoped.ScopedDevelopmentEditAdapter._unknown())
        self.assertEqual(self.target.read_bytes(), b'after\n')

    def test_forged_writer_success_is_unknown(self):
        with patch.object(edits, 'replace_text', return_value={
                'status': 'REPLACED_LOCAL_DEV', 'sha256': sha256(b'after\n').hexdigest()}):
            result = self.request()
        # No commit hook ran, but an unexpected success cannot be trusted.
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(self.target.read_bytes(), b'before\r\n')

    def test_actual_source_files_are_unchanged_by_adapter(self):
        package = scoped._REPO / 'src' / 'sora_commander_next'
        before = {p: sha256(p.read_bytes()).hexdigest() for p in package.glob('*.py')}
        live = package / 'stdio_main.py'
        adapter = scoped.ScopedDevelopmentEditAdapter(scoped.ScopedDevelopmentEditPolicy((str(live),)))
        self.ui.return_value = False
        with patch.object(edits, 'replace_text') as write:
            result = adapter.request(str(live), 'never written', before[live])
            self.assertEqual(result, {'status': 'DENIED'})
            write.assert_not_called()
        self.assertEqual(before, {p: sha256(p.read_bytes()).hexdigest() for p in package.glob('*.py')})
        self.assertNotIn('scoped_dev_edits', (package / 'stdio_main.py').read_text())
        self.assertNotIn('scoped_dev_edits', (package / 'mcp_adapter.py').read_text())


class ACLProtocolTests(unittest.TestCase):
    def test_acl_subprocess_errors_and_malformed_output_are_rejected(self):
        # Test the actual production ACL function, not the mock in policy tests.
        p = scoped._REPO / 'src' / 'sora_commander_next' / 'stdio_main.py'
        if os.name != 'nt':
            with self.assertRaises(ReadBoundaryError):
                scoped._acl_snapshot(p, 'sid')
            return
        for response in (SimpleNamespace(returncode=3, stdout=''),
                         SimpleNamespace(returncode=0, stdout='not json'),
                         SimpleNamespace(returncode=0, stdout='[]'),
                         SimpleNamespace(returncode=0, stdout='null')):
            with patch.object(scoped.subprocess, 'run', return_value=response), self.assertRaises(ReadBoundaryError):
                scoped._acl_snapshot(p, 'sid')
        with patch.object(scoped.subprocess, 'run', side_effect=subprocess.TimeoutExpired('powershell', 15)), self.assertRaises(ReadBoundaryError):
            scoped._acl_snapshot(p, 'sid')


class SharedGUITests(unittest.TestCase):
    def test_legacy_pilot_ui_keeps_complete_diff_and_45_second_timer(self):
        inserted, timers, callbacks = [], [], []
        class Widget:
            def __init__(self, *args, **kwargs):
                if kwargs.get('text') == 'Approve ONCE':
                    callbacks.append(kwargs['command'])
            def __getattr__(self, name):
                return lambda *args, **kwargs: None
            def insert(self, where, text):
                inserted.append(text)
            def after(self, milliseconds, callback):
                timers.append(milliseconds)
            def mainloop(self):
                callbacks[0]()
        fake = SimpleNamespace(**{name: Widget for name in ('Tk', 'Label', 'Text', 'Frame', 'Button', 'Scrollbar', 'Canvas')})
        # The deny timer is the remaining budget to the deadline; freeze the
        # clock so the full 45s budget is observed exactly.
        with patch.dict('sys.modules', {'tkinter': fake}), patch.object(edits, 'monotonic', return_value=100.0):
            self.assertIs(edits._show_edit_review({'path': 'disposable', 'diff': 'UNCHANGED LEGACY DIFF'}), True)
        self.assertEqual(inserted, ['disposable\n\n', 'UNCHANGED LEGACY DIFF'])
        # The 45s deny timer remains; 100ms is an audio-only Owner alert.
        self.assertEqual(timers, [45000, 100])

    def test_complete_text_and_timeout_are_bound_without_real_gui(self):
        inserted = []
        callback = []
        class Widget:
            def __init__(self, *args, **kwargs):
                if kwargs.get('text') == 'Approve ONCE':
                    callback.append(kwargs['command'])
            def __getattr__(self, name):
                return lambda *args, **kwargs: None
            def insert(self, where, content):
                inserted.append(content)
            def mainloop(self):
                callback[0]()
        fake = SimpleNamespace(**{name: Widget for name in ('Tk', 'Label', 'Text', 'Frame', 'Button', 'Scrollbar', 'Canvas')})
        for now, expected in ((10, True), (45, False)):
            callback.clear()
            with patch.dict('sys.modules', {'tkinter': fake}), patch.object(edits, 'monotonic', return_value=now):
                actual = edits._show_edit_review({'path': 'test', 'diff': 'DIFF'},
                                                complete_review='FULL\\r\\nTEXT', deadline=45)
            self.assertEqual(actual, expected)
            self.assertEqual(inserted[-1], 'FULL\\r\\nTEXT')


if __name__ == '__main__':
    unittest.main()
