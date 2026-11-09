"""Synthetic CREATE final admission checks: no GUI, disk mutation or Owner grant."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sora_commander_next import file_edits as edits


class ReachedExclusiveCreate(Exception):
    """Raised by the mocked x+b open: the writer was entered."""


class FinalCreateAdmissionTests(unittest.TestCase):
    def run_create(self, *, preview_elapsed=0, reject_guard=False):
        now = [100.0]
        digest = 'a' * 64
        preview = {'path': 'synthetic.txt', 'bytes': 3, 'new_sha256': digest,
                   'review': '"abc"', 'review_truncated': False}
        previews = [0]

        def make_preview(*_args):
            # The second preview runs inside create_text_exclusive, after the
            # caller's final checks and before the exclusive open.
            previews[0] += 1
            if previews[0] >= 2:
                now[0] += preview_elapsed
            return preview

        guard = Mock()
        calls = [0]

        def verify():
            calls[0] += 1
            if reject_guard and calls[0] >= 3:
                raise PermissionError('synthetic identity change')

        guard.verify.side_effect = verify
        target = Mock()
        target.open.return_value.__enter__ = Mock(side_effect=ReachedExclusiveCreate)
        target.open.return_value.__exit__ = Mock(return_value=False)
        tk = Mock()
        callbacks = {}

        def actions(_tk, _card, reject, approve):
            callbacks['approve'] = approve

        tk.Tk.return_value.mainloop.side_effect = lambda: callbacks['approve']()
        with patch.dict(sys.modules, {'tkinter': tk}), \
                patch('sora_commander_next.windows_owner.OwnerSessionGuard', return_value=guard), \
                patch.object(edits, 'preview_text_create', side_effect=make_preview), \
                patch.object(edits, '_create_target', return_value=target), \
                patch.object(edits, 'monotonic', side_effect=lambda: now[0]), \
                patch.multiple('sora_commander_next.approval_visual_theme',
                               window=Mock(), header=Mock(), notice=Mock(),
                               viewer=Mock(return_value=Mock()), review_actions=actions):
            try:
                result = edits.review_and_create_local('synthetic.txt', (), 'abc', digest)
            except edits.WriteNotStartedError:
                result = {'status': 'NOT_STARTED'}
            except ReachedExclusiveCreate:
                result = {'status': 'WRITER_ENTERED'}
        return result, target

    def test_expiry_before_exclusive_open_never_enters_writer(self):
        result, target = self.run_create(preview_elapsed=45)
        self.assertEqual(result, {'status': 'NOT_STARTED'})
        target.open.assert_not_called()

    def test_final_owner_failure_before_exclusive_open_never_enters_writer(self):
        result, target = self.run_create(reject_guard=True)
        self.assertEqual(result, {'status': 'NOT_STARTED'})
        target.open.assert_not_called()

    def test_timely_approval_enters_writer_once(self):
        result, target = self.run_create(preview_elapsed=44.999)
        self.assertEqual(result, {'status': 'WRITER_ENTERED'})
        target.open.assert_called_once_with('x+b')


if __name__ == '__main__':
    unittest.main()
