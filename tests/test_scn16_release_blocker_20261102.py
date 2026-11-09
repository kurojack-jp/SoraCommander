"""Synthetic final admission checks: no GUI, disk mutation or Owner grant."""
import sys
import unittest
from pathlib import Path
from unittest.mock import ANY, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sora_commander_next import file_edits as edits
from sora_commander_next.owner_review_file_view import create_file_view


class FinalEditAdmissionTests(unittest.TestCase):
    def run_edit(self, *, guard_elapsed=0, staging_elapsed=0, reject_guard=False):
        now = [100.0]
        preview = {'path': 'synthetic.py', 'diff': '-old\n+new',
                   'changed': True, 'diff_truncated': False}
        guard = Mock()
        calls = [0]

        def verify():
            calls[0] += 1
            if calls[0] >= 2:
                now[0] += guard_elapsed
            if reject_guard and calls[0] >= 3:
                raise PermissionError('synthetic identity change')

        guard.verify.side_effect = verify
        effect = Mock(return_value={'status': 'REPLACED_LOCAL_DEV'})

        def replace(*args, **kwargs):
            now[0] += staging_elapsed
            if kwargs.get('_before_commit') is not None:
                kwargs['_before_commit'](None, None)
            return effect()

        with patch('sora_commander_next.windows_owner.OwnerSessionGuard', return_value=guard), \
                patch.object(edits, 'preview_text_edit', return_value=preview), \
                patch.object(edits, '_show_edit_review', return_value=True) as gui, \
                patch.object(edits, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(edits, 'replace_text', side_effect=replace) as write:
            try:
                result = edits.review_and_replace_local('synthetic.py', (), 'new', 'a' * 64)
            except edits.WriteNotStartedError:
                result = {'status': 'NOT_STARTED'}
        return result, effect, write, gui

    def test_expiry_during_owner_recheck_never_enters_writer(self):
        result, effect, write, gui = self.run_edit(guard_elapsed=45)
        self.assertEqual(result, {'status': 'DENIED'})
        write.assert_not_called()
        effect.assert_not_called()
        self.assertEqual(gui.call_args.kwargs['deadline'], 145.0)

    def test_expiry_while_staging_blocks_replace(self):
        result, effect, _, _ = self.run_edit(staging_elapsed=45)
        self.assertEqual(result, {'status': 'NOT_STARTED'})
        effect.assert_not_called()

    def test_final_owner_failure_blocks_replace(self):
        result, effect, _, _ = self.run_edit(reject_guard=True)
        self.assertEqual(result, {'status': 'NOT_STARTED'})
        effect.assert_not_called()

    def test_timely_approval_writes_once(self):
        result, effect, _, _ = self.run_edit(staging_elapsed=44.999)
        self.assertEqual(result, {'status': 'REPLACED_LOCAL_DEV'})
        effect.assert_called_once_with()


class CreatePathDisplayTests(unittest.TestCase):
    def test_active_and_candidate_escape_path_without_changing_write_input(self):
        path = 'D:/synthetic/parent\u202E\u200B\x1b/owner-edit-pilot-safe.txt'
        digest = 'a' * 64
        preview = {'path': path, 'bytes': 3, 'new_sha256': digest,
                   'review': '"abc"', 'review_truncated': False}
        tk, pane = Mock(), Mock()
        callbacks = {}

        def actions(_tk, _card, reject, approve):
            callbacks['approve'] = approve

        tk.Tk.return_value.mainloop.side_effect = lambda: callbacks['approve']()
        with patch.dict(sys.modules, {'tkinter': tk}), \
                patch('sora_commander_next.windows_owner.OwnerSessionGuard'), \
                patch.object(edits, 'preview_text_create', return_value=preview), \
                patch.object(edits, 'create_text_exclusive') as write, \
                patch.multiple('sora_commander_next.approval_visual_theme',
                               window=Mock(), header=Mock(), notice=Mock(),
                               viewer=Mock(return_value=pane), review_actions=actions):
            edits.review_and_create_local(path, (), 'abc', digest)
        body = pane.insert.call_args.args[1]
        self.assertIn(r'parent\u202E\u200B\u001B', body)
        self.assertEqual(body, create_file_view(preview, digest).body)
        write.assert_called_once_with(path, (), 'abc', digest, _before_create=ANY)


if __name__ == '__main__':
    unittest.main()
