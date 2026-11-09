"""Local-only concurrency and timeout-result regression tests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hashlib import sha256
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from threading import Event
from unittest import TestCase
from unittest.mock import patch
from sora_commander_next.edit_review_pilot import EditReviewPilot

class EditReviewConcurrencyTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
        self.temp = self.enterContext(inherited_test_temp())
        root = Path(self.temp)
        self.path = root / 'owner-edit-pilot-concurrency.txt'
        self.path.write_bytes(b'before\n')
        self.pilot = EditReviewPilot(root)
        self.digest = sha256(b'before\n').hexdigest()

    def test_second_request_busy_while_first_awaits_owner(self):
        entered, release = Event(), Event()
        def review(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test cleanup timeout')
            return {'status': 'DENIED'}
        with patch('sora_commander_next.edit_review_pilot.review_and_replace_local', side_effect=review):
            first = self.pilot.request(str(self.path), 'first\n', self.digest)
            try:
                self.assertTrue(entered.wait(2))
                second = self.pilot.request(str(self.path), 'second\n', self.digest)
                self.assertEqual(first['status'], 'REVIEW')
                self.assertEqual(second['status'], 'BUSY')
                self.assertEqual(self.pilot.status(first['pending_id'])['status'], 'PENDING')
                self.assertEqual(self.path.read_bytes(), b'before\n')
            finally:
                release.set()
            for _ in range(100):
                if self.pilot.status(first['pending_id'])['status'] != 'PENDING':
                    break
                from time import sleep
                sleep(0.01)
            self.assertEqual(self.pilot.status(first['pending_id'])['status'], 'DENIED')
            self.assertEqual(self.path.read_bytes(), b'before\n')

    def test_expired_owner_review_reports_denied_and_keeps_file(self):
        # Local UI's 45-second timeout produces DENIED; avoid sleeping 45s.
        with patch('sora_commander_next.edit_review_pilot.review_and_replace_local', return_value={'status': 'DENIED'}):
            result = self.pilot.request(str(self.path), 'after\n', self.digest)
            from time import sleep
            for _ in range(100):
                if self.pilot.status(result['pending_id'])['status'] != 'PENDING':
                    break
                sleep(0.01)
        self.assertEqual(self.pilot.status(result['pending_id'])['status'], 'DENIED')
        self.assertEqual(self.path.read_bytes(), b'before\n')
