"""Disposable edit pilot: stale file after review request must not be overwritten."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hashlib import sha256
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from time import sleep
from unittest import TestCase
from unittest.mock import patch
from sora_commander_next.edit_review_pilot import EditReviewPilot
from sora_commander_next.file_edits import replace_text

class ConcurrentMutationTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
    def test_mutation_during_review_rejects_final_commit(self):
        with inherited_test_temp() as directory:
            root = Path(directory)
            target = root / 'owner-edit-pilot-stale.txt'
            target.write_bytes(b'original\n')
            initial_digest = sha256(b'original\n').hexdigest()
            pilot = EditReviewPilot(root)
            def simulated_owner_approval(path, roots, content, digest):
                # Independent writer modifies the file while owner review is pending.
                target.write_bytes(b'edited by another process\n')
                return replace_text(path, roots, content, digest)
            with patch('sora_commander_next.edit_review_pilot.review_and_replace_local', side_effect=simulated_owner_approval):
                requested = pilot.request(str(target), 'proposed change\n', initial_digest)
                self.assertEqual(requested['status'], 'REVIEW')
                for _ in range(200):
                    outcome = pilot.status(requested['pending_id'])
                    if outcome['status'] != 'PENDING':
                        break
                    sleep(0.01)
            # Revalidated stale digest is rejected BEFORE os.replace.
            # This specific failure cannot have written proposed bytes.
            self.assertEqual(outcome, {
                'status': 'ERROR', 'error': 'WriteNotStarted',
            })
            self.assertEqual(target.read_bytes(), b'edited by another process\n')
            self.assertIsNone(pilot._active)
