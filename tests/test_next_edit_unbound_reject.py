"""Unbound local edit worker callbacks must not display approval or steal a live review."""
import sys
from hashlib import sha256
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sora_commander_next.edit_review_pilot import EditReviewPilot


class UnboundEditTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())

    def test_unknown_request_id_refuses_approval(self):
        with inherited_test_temp() as root:
            path = Path(root) / "owner-edit-pilot-case.txt"
            path.write_bytes(b"old")
            pilot = EditReviewPilot(root)
            unknown = "a" * 32
            with patch("sora_commander_next.edit_review_pilot.review_and_replace_local") as review:
                pilot._execute(unknown, str(path), "new",
                               sha256(b"old").hexdigest(), sha256(b"new").hexdigest())
                review.assert_not_called()
            self.assertEqual(pilot.status(unknown)["status"], "ERROR")
            self.assertEqual(path.read_bytes(), b"old")

    def test_unknown_callback_cannot_clear_unrelated_pending_review(self):
        with inherited_test_temp() as root:
            path = Path(root) / "owner-edit-pilot-case.txt"
            path.write_bytes(b"old")
            pilot = EditReviewPilot(root)
            live, unknown = "b" * 32, "c" * 32
            pilot._active = live
            pilot._results[live] = {"status": "PENDING", "target": str(path),
                "old_sha256": sha256(b"old").hexdigest(),
                "new_sha256": sha256(b"approved").hexdigest()}
            with patch("sora_commander_next.edit_review_pilot.review_and_replace_local") as review:
                pilot._execute(unknown, str(path), "new",
                               sha256(b"old").hexdigest(), sha256(b"new").hexdigest())
                review.assert_not_called()
            self.assertEqual(pilot._active, live)
            self.assertEqual(pilot.status(live)["status"], "PENDING")
            self.assertEqual(path.read_bytes(), b"old")

    def test_unknown_mismatched_proposal_cannot_clear_other_pending_review(self):
        with inherited_test_temp() as root:
            path = Path(root) / "owner-edit-pilot-case.txt"
            path.write_bytes(b"old")
            pilot = EditReviewPilot(root)
            live, unknown = "d" * 32, "e" * 32
            pilot._active = live
            pilot._results[live] = {"status": "PENDING", "target": str(path),
                "old_sha256": sha256(b"old").hexdigest(),
                "new_sha256": sha256(b"approved").hexdigest()}
            with patch("sora_commander_next.edit_review_pilot.review_and_replace_local") as review:
                pilot._execute(unknown, str(path), "unapproved",
                               sha256(b"old").hexdigest(), sha256(b"approved").hexdigest())
                review.assert_not_called()
            self.assertEqual(pilot._active, live)
            self.assertEqual(pilot.status(live)["status"], "PENDING")
            self.assertEqual(path.read_bytes(), b"old")
