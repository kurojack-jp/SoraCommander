"""Check the disposable edit request binds target, digests and review identifier."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from hashlib import sha256
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from time import sleep
from threading import Event
from unittest import TestCase
from unittest.mock import patch
from sora_commander_next.edit_review_pilot import EditReviewPilot

class RequestBindingTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
    def test_pending_request_carries_target_and_both_digests(self):
        with inherited_test_temp() as root:
            target=Path(root)/'owner-edit-pilot-binding.txt'
            target.write_bytes(b'old')
            pilot=EditReviewPilot(root)
            entered=Event(); release=Event()
            def review(*args):
                entered.set(); release.wait(3)
                return {'status':'DENIED'}
            with patch('sora_commander_next.edit_review_pilot.review_and_replace_local',side_effect=review):
                response=pilot.request(str(target),'new',sha256(b'old').hexdigest())
                try:
                    self.assertTrue(entered.wait(2))
                    state=pilot.status(response['pending_id'])
                    self.assertEqual(state['target'],str(target))
                    self.assertEqual(state['old_sha256'],sha256(b'old').hexdigest())
                    self.assertEqual(state['new_sha256'],sha256(b'new').hexdigest())
                finally:
                    release.set()

    def test_stale_file_before_review_prevents_prompt(self):
        with inherited_test_temp() as root:
            target=Path(root)/'owner-edit-pilot-stale.txt'
            target.write_bytes(b'other')
            pilot=EditReviewPilot(root)
            token = 'f'*32
            pilot._active = token
            pilot._results[token] = {'status':'PENDING', 'target':str(target),
                'old_sha256':sha256(b'old').hexdigest(),
                'new_sha256':sha256(b'new').hexdigest()}
            with patch('sora_commander_next.edit_review_pilot.review_and_replace_local') as review:
                pilot._execute(token,str(target),'new',sha256(b'old').hexdigest(),sha256(b'new').hexdigest())
                review.assert_not_called()
            self.assertEqual(pilot.status('f'*32)['error'],'ValueError')
            self.assertEqual(target.read_bytes(),b'other')
