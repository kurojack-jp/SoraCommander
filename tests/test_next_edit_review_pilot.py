"""Local-only regression tests for disposable edit review."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hashlib import sha256
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from unittest import TestCase
from unittest.mock import patch
import time
from sora_commander_next.edit_review_pilot import EditReviewPilot
from sora_commander_next.file_reads import ReadBoundaryError

class InlineThread:
    def __init__(self, target, args, **kwargs):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)

class PilotTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
        self.temp = self.enterContext(inherited_test_temp())
        self.root = Path(self.temp)
        self.path = self.root / 'owner-edit-pilot-example.txt'
        self.path.write_bytes(b'before\n')
        self.digest = sha256(b'before\n').hexdigest()
        self.pilot = EditReviewPilot(self.root)

    def test_rejection_does_not_change_file(self):
        with patch('sora_commander_next.edit_review_pilot.review_and_replace_local', return_value={'status': 'DENIED'}):
            result = self.pilot.request(str(self.path), 'after\n', self.digest)
            for _ in range(100):
                if self.pilot.status(result['pending_id'])['status'] != 'PENDING':
                    break
                time.sleep(0.01)
        self.assertEqual(self.pilot.status(result['pending_id'])['status'], 'DENIED')
        self.assertEqual(self.path.read_bytes(), b'before\n')

    def test_thread_start_failure_releases_admission(self):
        with patch('sora_commander_next.edit_review_pilot.Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError('test start failure')
            result = self.pilot.request(str(self.path), 'after\n', self.digest)
        self.assertEqual(result, {'status': 'ERROR', 'error': 'ReviewStartFailed'})
        self.assertIsNone(self.pilot._active)
        self.assertEqual(self.path.read_bytes(), b'before\n')
        with patch('sora_commander_next.edit_review_pilot.review_and_replace_local',
                   return_value={'status': 'DENIED'}):
            again = self.pilot.request(str(self.path), 'after\n', self.digest)
            self.assertEqual(again['status'], 'REVIEW')

    def test_repeated_thread_failures_keep_history_bounded(self):
        with patch('sora_commander_next.edit_review_pilot.Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError('thread unavailable')
            for _ in range(80):
                result = self.pilot.request(str(self.path), 'after\n', self.digest)
                self.assertEqual(result['status'], 'ERROR')
                self.assertIsNone(self.pilot._active)
        self.assertLessEqual(len(self.pilot._results), 64)
        self.assertEqual(self.path.read_bytes(), b'before\n')

    def test_stale_digest_is_rejected(self):
        with self.assertRaises(ReadBoundaryError):
            self.pilot.request(str(self.path), 'after\n', sha256(b'wrong').hexdigest())
        self.assertEqual(self.path.read_bytes(), b'before\n')

    def test_unapproved_path_is_rejected(self):
        other = self.root / 'other.txt'
        other.write_bytes(b'before\n')
        with self.assertRaises(PermissionError):
            self.pilot.request(str(other), 'after\n', self.digest)

    def test_identical_content_does_not_request_approval(self):
        result = self.pilot.request(str(self.path), 'before\n', self.digest)
        self.assertEqual(result['status'], 'UNCHANGED')
