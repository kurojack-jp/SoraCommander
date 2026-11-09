"""Post-write mismatch must not overwrite third-party updates during recovery."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from hashlib import sha256
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from unittest import TestCase
from unittest.mock import patch
from sora_commander_next.edit_review_pilot import EditReviewPilot

class RecoveryPolicyTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
    def test_mismatch_does_not_rollback_other_writers_bytes(self):
        with inherited_test_temp() as directory:
            target=Path(directory)/'owner-edit-pilot-recovery.txt'
            target.write_bytes(b'original')
            pilot=EditReviewPilot(directory)
            def third_party_update(*args):
                target.write_bytes(b'third party')
                return {'status':'REPLACED_LOCAL_DEV',
                        'sha256':sha256(b'proposed').hexdigest()}
            token='a'*32
            digest=sha256(b'original').hexdigest()
            pilot._active=token
            pilot._results[token]={'status':'PENDING','target':str(target),
                'old_sha256':digest,'new_sha256':sha256(b'proposed').hexdigest()}
            with patch('sora_commander_next.edit_review_pilot.review_and_replace_local',side_effect=third_party_update):
                pilot._execute(token,str(target),'proposed',digest)
            result=pilot.status('a'*32)
            self.assertEqual(result['error'],'PostWriteVerificationFailed')
            self.assertEqual(result['recovery'],'MANUAL_REVIEW_REQUIRED')
            self.assertFalse(result['auto_rollback'])
            self.assertEqual(target.read_bytes(),b'third party')
