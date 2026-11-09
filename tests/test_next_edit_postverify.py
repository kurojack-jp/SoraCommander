"""Disposable Owner-edit pilot: target scoping and post-write attestation."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hashlib import sha256
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from unittest import TestCase
from unittest.mock import patch
from sora_commander_next.edit_review_pilot import EditReviewPilot

class PostVerifyTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())

    def _bind(self, pilot, token, path, old, new):
        pilot._active = token
        pilot._results[token] = {'status': 'PENDING', 'target': str(path),
            'old_sha256': sha256(old).hexdigest(),
            'new_sha256': sha256(new).hexdigest()}

    def test_confirm_written_bytes_and_digest(self):
        with inherited_test_temp() as folder:
            root=Path(folder); p=root/'owner-edit-pilot-ok.txt';p.write_bytes(b'old')
            pilot=EditReviewPilot(root)
            self._bind(pilot, 'a'*32, p, b'old', b'new')
            def write(*args):
                p.write_bytes(b'new')
                return {'status':'REPLACED_LOCAL_DEV','sha256':sha256(b'new').hexdigest()}
            with patch('sora_commander_next.edit_review_pilot.review_and_replace_local',side_effect=write):
                pilot._execute('a'*32,str(p),'new',sha256(b'old').hexdigest())
            self.assertEqual(pilot.status('a'*32)['sha256'],sha256(b'new').hexdigest())

    def test_report_mismatch_when_actual_file_differs(self):
        with inherited_test_temp() as folder:
            root=Path(folder);p=root/'owner-edit-pilot-bad.txt';p.write_bytes(b'old')
            pilot=EditReviewPilot(root)
            self._bind(pilot, 'b'*32, p, b'old', b'new')
            with patch('sora_commander_next.edit_review_pilot.review_and_replace_local',return_value={'status':'REPLACED_LOCAL_DEV','sha256':sha256(b'new').hexdigest()}):
                pilot._execute('b'*32,str(p),'new',sha256(b'old').hexdigest())
            self.assertEqual(pilot.status('b'*32)['error'],'PostWriteVerificationFailed')
            self.assertEqual(p.read_bytes(),b'old')

    def test_symlink_does_not_pass_target_check(self):
        with inherited_test_temp() as folder:
            root=Path(folder);target=root/'other.txt';target.write_bytes(b'old')
            link=root/'owner-edit-pilot-link.txt'
            try:
                link.symlink_to(target)
            except (OSError,NotImplementedError):
                self.skipTest('symlink privilege unavailable')
            try:
                with self.assertRaises(PermissionError):
                    EditReviewPilot(root)._target(str(link))
            finally:
                # The inherited fixture refuses links; remove ours before it closes.
                link.unlink()
