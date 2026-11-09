"""Queued approval cannot silently switch targets or proposal digests."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from hashlib import sha256
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from sora_commander_next.edit_review_pilot import EditReviewPilot

class BindingMismatchTests(TestCase):
    def test_mismatched_target_is_rejected_before_review(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            first=root/'owner-edit-pilot-first.txt'
            other=root/'owner-edit-pilot-other.txt'
            first.write_bytes(b'old');other.write_bytes(b'old')
            pilot=EditReviewPilot(root)
            token='d'*32
            digest=sha256(b'old').hexdigest()
            proposed=sha256(b'new').hexdigest()
            pilot._active=token
            pilot._results[token]={'status':'PENDING','target':str(first),
                                   'old_sha256':digest,'new_sha256':proposed}
            with patch('sora_commander_next.edit_review_pilot.review_and_replace_local') as review:
                pilot._execute(token,str(other),'new',digest,proposed)
                review.assert_not_called()
            self.assertEqual(pilot.status(token)['status'],'ERROR')
            self.assertEqual(first.read_bytes(),b'old')
            self.assertEqual(other.read_bytes(),b'old')
