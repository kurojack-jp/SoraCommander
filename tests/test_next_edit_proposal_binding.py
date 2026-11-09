"""Local disposable edit proposals must match their requested digest."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hashlib import sha256
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from unittest import TestCase
from unittest.mock import patch
from sora_commander_next.edit_review_pilot import EditReviewPilot

class ProposalBindingTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
    def test_changed_proposal_is_rejected_without_review(self):
        with inherited_test_temp() as folder:
            target=Path(folder)/'owner-edit-pilot-binding.txt'
            target.write_bytes(b'original')
            pilot=EditReviewPilot(folder)
            with patch('sora_commander_next.edit_review_pilot.review_and_replace_local') as review:
                pilot._execute('c'*32,str(target),'unapproved',sha256(b'original').hexdigest(),sha256(b'approved').hexdigest())
                review.assert_not_called()
            self.assertEqual(pilot.status('c'*32)['error'],'ProposalDigestMismatch')
            self.assertEqual(target.read_bytes(),b'original')
