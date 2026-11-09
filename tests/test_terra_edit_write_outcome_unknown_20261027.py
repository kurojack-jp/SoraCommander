"""Terra P2: a possibly committed disposable edit must remain HOLD after unknown result.

No production file edited. All successful write responses and I/O errors are
synthetic; temporary fixtures are not modified by the mocked write operation.
"""
from hashlib import sha256
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from unittest import TestCase
from unittest.mock import patch
import time

from sora_commander_next.edit_review_pilot import EditReviewPilot


class PilotPostWriteUnknownTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
        temp = self.enterContext(inherited_test_temp())
        self.root = Path(temp)
        self.file = self.root / "owner-edit-pilot-terra.txt"
        self.file.write_bytes(b"before\n")
        self.old_digest = sha256(b"before\n").hexdigest()
        self.new_digest = sha256(b"after\n").hexdigest()
        self.pilot = EditReviewPilot(self.root)

    def wait_result(self, pending_id):
        for _ in range(300):
            result = self.pilot.status(pending_id)
            if result["status"] != "PENDING":
                return result
            time.sleep(0.005)
        self.fail("pilot thread did not finish")

    def assert_hold(self, pending_id, error):
        result = self.wait_result(pending_id)
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error"], error)
        self.assertEqual(result["recovery"], "MANUAL_REVIEW_REQUIRED")
        self.assertIs(result["auto_rollback"], False)
        self.assertIs(result["auto_retry"], False)
        self.assertEqual(self.pilot._active, pending_id)
        again = self.pilot.request(str(self.file), "after\n", self.old_digest)
        self.assertEqual(again["status"], "BUSY")

    def test_post_write_read_permission_error_holds_next_request(self):
        original = Path.read_bytes
        state = {"write_acknowledged": False}

        def read(path):
            if path == self.file and state["write_acknowledged"]:
                raise PermissionError("simulated post-write read failure")
            return original(path)

        def acknowledge(*_args, **_kwargs):
            state["write_acknowledged"] = True
            return {"status": "REPLACED_LOCAL_DEV", "sha256": self.new_digest}

        with (patch("sora_commander_next.edit_review_pilot.review_and_replace_local",
                    side_effect=acknowledge),
              patch.object(Path, "read_bytes", read)):
            submitted = self.pilot.request(str(self.file), "after\n", self.old_digest)
            self.assertEqual(submitted["status"], "REVIEW")
            self.wait_result(submitted["pending_id"])
        self.assert_hold(submitted["pending_id"], "PostWriteVerificationFailed")
        self.assertEqual(self.file.read_bytes(), b"before\n")

    def test_write_call_error_is_ambiguous_not_safe_to_retry(self):
        with patch("sora_commander_next.edit_review_pilot.review_and_replace_local",
                   side_effect=OSError("simulated uncertain syscall completion")):
            submitted = self.pilot.request(str(self.file), "after\n", self.old_digest)
            self.assert_hold(submitted["pending_id"], "WriteOutcomeUnknown")

    def test_explicit_prewrite_error_releases_pending_without_recovery(self):
        from sora_commander_next.file_edits import WriteNotStartedError
        with patch("sora_commander_next.edit_review_pilot.review_and_replace_local",
                   side_effect=WriteNotStartedError("synthetic prewrite rejection")):
            submitted = self.pilot.request(str(self.file), "after\n", self.old_digest)
            result = self.wait_result(submitted["pending_id"])
        self.assertEqual(result, {"status": "ERROR", "error": "WriteNotStarted"})
        self.assertIsNone(self.pilot._active)
        self.assertEqual(self.file.read_bytes(), b"before\n")

    def test_untyped_permission_error_remains_ambiguous_and_pinned(self):
        with patch("sora_commander_next.edit_review_pilot.review_and_replace_local",
                   side_effect=PermissionError("unknown write stage")):
            submitted = self.pilot.request(str(self.file), "after\n", self.old_digest)
            self.assert_hold(submitted["pending_id"], "WriteOutcomeUnknown")

    def test_post_create_verification_error_also_holds(self):
        pilot = EditReviewPilot(self.root, enable_create=True)
        target = self.root / "owner-edit-pilot-created.txt"
        with (patch("sora_commander_next.edit_review_pilot.preview_text_create",
                    return_value={"path": str(target), "new_sha256": self.new_digest,
                                  "review_truncated": False}),
              patch("sora_commander_next.edit_review_pilot.review_and_create_local",
                    return_value={"status": "CREATED_LOCAL_DEV",
                                  "sha256": self.new_digest}),
              patch("sora_commander_next.edit_review_pilot._verify_created_text",
                    side_effect=PermissionError("simulated verify failure"))):
            submitted = pilot.request_create(str(target), "after\n")
            self.assertEqual(submitted["status"], "REVIEW")
            for _ in range(300):
                status = pilot.status(submitted["pending_id"])
                if status["status"] != "PENDING":
                    break
                time.sleep(0.005)
            self.assertEqual(status["error"], "CreateVerificationError")
            self.assertEqual(status["recovery"], "MANUAL_REVIEW_REQUIRED")
            self.assertIs(status["auto_retry"], False)
            self.assertIs(status["auto_rollback"], False)
            self.assertEqual(pilot._active, submitted["pending_id"])
        self.assertFalse(target.exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
