"""V2 local Owner review framing tests; never show GUI or start a process."""
from pathlib import Path
from unittest.mock import Mock, patch
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sora_commander_next.commands import Command
from sora_commander_next.approval_v2_owner_local import (
    LocalOwnerV2Pilot, make_review_frame,
)
from sora_commander_next.owner_approval_dialog_v2 import (
    parse_review, verify_review_reply_data_only,
)
import json


def sample():
    return Command(sys.executable, ("--version",),
                   str(Path(__file__).resolve().parents[1]),
                   timeout=10, output_limit=4096)


class OwnerV2LocalTests(unittest.TestCase):
    def test_frame_binds_real_command_without_internal_source(self):
        raw, op = make_review_frame(
            sample(), owner_sid="S-1-5-21-TEST",
            machine_id="DESKTOP-TEST", nonce="a" * 64,
            review_id="review-1", allow_trusted=True)
        data = parse_review(raw)
        self.assertEqual(data["spec_sha256"], op.digest)
        self.assertEqual(data["argv"], ["--version"])
        self.assertEqual(data["owner_label"], "S-1-5-21-TEST")
        self.assertTrue(data["allow_trusted"])
        self.assertFalse(data["unattended"])
        self.assertEqual(data["timeout_ms"], 10000)
        self.assertEqual(data["output_limit"], 4096)

    def test_valid_ui_answer_bound_to_exact_frame(self):
        raw, op = make_review_frame(
            sample(), owner_sid="owner", machine_id="machine",
            nonce="b" * 64, review_id="review-2",
            allow_trusted=True)
        frame = parse_review(raw)
        answer = {
            "nonce": frame["nonce"], "review_id": frame["review_id"],
            "spec_sha256": op.digest, "decision": "TRUSTED"}
        encoded = json.dumps(answer).encode("ascii") + b"\n"
        self.assertEqual(verify_review_reply_data_only(frame, encoded), "TRUSTED")

    def test_without_explicit_trusted_offer_cannot_upgrade(self):
        raw, op = make_review_frame(
            sample(), owner_sid="owner", machine_id="machine",
            nonce="c" * 64, review_id="review-3")
        frame = parse_review(raw)
        answer = {
            "nonce": frame["nonce"], "review_id": frame["review_id"],
            "spec_sha256": op.digest, "decision": "TRUSTED"}
        with self.assertRaises(ValueError):
            verify_review_reply_data_only(
                frame, json.dumps(answer).encode("ascii") + b"\n")

    def test_request_change_alters_frame_fingerprint(self):
        first, op1 = make_review_frame(
            sample(), owner_sid="owner", machine_id="machine")
        altered = Command(sys.executable, ("-V",),
                          str(Path(__file__).resolve().parents[1]))
        _, op2 = make_review_frame(
            altered, owner_sid="owner", machine_id="machine")
        self.assertNotEqual(op1.digest, op2.digest)

    def test_invalid_owner_gate_never_launches_ui(self):
        # Synthetic negative test: no real Owner click or OS attestation.
        pilot = object.__new__(LocalOwnerV2Pilot)
        pilot._guard = Mock()
        pilot._guard.verify.side_effect = PermissionError("not Owner")
        with patch("sora_commander_next.approval_v2_owner_local.subprocess.Popen") as launch:
            result = pilot.review_once_local(sample(), allow_trusted=True)
        self.assertEqual(result.status, "ERROR")
        launch.assert_not_called()

    def test_frame_invalid_optional_mode_rejected(self):
        with self.assertRaises(ValueError):
            make_review_frame(sample(), owner_sid="owner",
                              machine_id="machine", allow_trusted=1)
        with self.assertRaises(ValueError):
            make_review_frame(sample(), owner_sid="owner",
                              machine_id="machine", seconds=0)

    def _mock_review(self, decision="EXPIRED", failure=None):
        pilot = object.__new__(LocalOwnerV2Pilot)
        pilot._guard = Mock(sid="synthetic-owner")
        child = Mock(pid=123, returncode=0, stdin=None, stdout=None)
        child.poll.return_value = 0
        def reply(*, input, timeout):
            frame = parse_review(input)
            data = {key: frame[key] for key in ("nonce", "review_id", "spec_sha256")}
            data["decision"] = decision
            if failure in ("nonce", "review_id", "spec_sha256"):
                data[failure] = "wrong"
            if failure == "process":
                child.returncode = 1
            if failure == "transport":
                raise OSError("synthetic pipe disconnect")
            raw = json.dumps(data).encode("ascii") + b"\n"
            return (b"bad\n" if failure == "malformed" else raw), None
        child.communicate.side_effect = reply
        with patch("sora_commander_next.windows_job.WindowsJob") as job, \
             patch("sora_commander_next.windows_session_events.OwnerSessionEventLatch") as latch, \
             patch("sora_commander_next.approval_v2_owner_local.subprocess.Popen", return_value=child) as launch:
            if failure == "WTS":
                latch.return_value.start.return_value.verify.side_effect = PermissionError("synthetic WTS")
            if failure == "identity":
                pilot._guard.verify.side_effect = PermissionError("synthetic identity")
            result = pilot.review_once_local(sample(), allow_trusted=True)
            if failure in ("WTS", "identity"):
                launch.assert_not_called()
            else:
                launch.assert_called_once()
                self.assertEqual(launch.call_args.args[0][-1], str(Path(__file__).resolve().parents[1] / "src" / "sora_commander_next" / "owner_approval_dialog_v2.py"))
                self.assertIs(launch.call_args.kwargs["shell"], False)
                job.return_value.attach_and_resume.assert_called_once_with(child)
        return result

    def test_mock_expiry_and_rejection_are_distinct_inert_data(self):
        for decision, status, choice in (
                ("EXPIRED", "EXPIRED_DATA_ONLY", None),
                ("DENY", "DENIED_DATA_ONLY", None),
                ("ONCE", "LOCAL_CHOICE_DATA_ONLY", "ONCE"),
                ("TRUSTED", "LOCAL_CHOICE_DATA_ONLY", "TRUSTED")):
            with self.subTest(decision=decision):
                result = self._mock_review(decision)
                self.assertEqual(result.status, status)
                self.assertEqual(result.choice, choice)
                self.assertIsNotNone(result.review_id)
                self.assertEqual(len(result.review_sha256), 64)
                self.assertNotIn(result.status, ("TIMED_OUT", "NOT_STARTED"))

    def test_mock_invalid_expiry_and_ui_failures_remain_error(self):
        for failure in ("nonce", "review_id", "spec_sha256", "process", "transport", "malformed", "WTS", "identity"):
            with self.subTest(failure=failure):
                result = self._mock_review(failure=failure)
                self.assertEqual(result.status, "ERROR")
                self.assertIsNone(result.choice)
                self.assertIsNone(result.review_id)

    def test_real_windows_child_pipe_synthetic_expired_no_grant(self):
        fixture = Path(__file__).parent / "fixtures" / "v2_dialog_synthetic_reply.py"
        with patch("sora_commander_next.approval_v2_owner_local._DIALOG", fixture), patch.dict(
                "os.environ", {"SORA_V2_TEST_CHOICE": "EXPIRED"}):
            result = LocalOwnerV2Pilot().review_once_local(sample(), allow_trusted=True)
        self.assertEqual(result.status, "EXPIRED_DATA_ONLY")
        self.assertIsNone(result.choice)
        self.assertIsNotNone(result.review_id)
        self.assertEqual(len(result.review_sha256), 64)
        self.assertEqual(len(result.spec_sha256), 64)

    def test_real_windows_child_pipe_synthetic_once_data_only(self):
        """Real WTS/Win32 child + anonymous pipe, synthetic output, NO click."""
        fixture = Path(__file__).parent / "fixtures" / "v2_dialog_synthetic_reply.py"
        command = sample()
        with patch("sora_commander_next.approval_v2_owner_local._DIALOG", fixture):
            pilot = LocalOwnerV2Pilot()
            result = pilot.review_once_local(command, allow_trusted=True)
        self.assertEqual(result.status, "LOCAL_CHOICE_DATA_ONLY", result)
        self.assertEqual(result.choice, "ONCE")
        self.assertEqual(result.request_id, command.request_id)
        self.assertIsNotNone(result.review_sha256)
        self.assertEqual(len(result.spec_sha256), 64)

    def test_real_windows_child_pipe_synthetic_deny_no_grant(self):
        fixture = Path(__file__).parent / "fixtures" / "v2_dialog_synthetic_reply.py"
        with patch("sora_commander_next.approval_v2_owner_local._DIALOG", fixture), patch.dict(
                "os.environ", {"SORA_V2_TEST_CHOICE": "DENY"}):
            result = LocalOwnerV2Pilot().review_once_local(sample(), allow_trusted=True)
        self.assertEqual(result.status, "DENIED_DATA_ONLY")
        self.assertIsNone(result.choice)
        self.assertIsNotNone(result.review_id)
        self.assertEqual(len(result.review_sha256), 64)
        self.assertEqual(len(result.spec_sha256), 64)

    def test_real_windows_child_pipe_synthetic_wrong_nonce_denied(self):
        fixture = Path(__file__).parent / "fixtures" / "v2_dialog_synthetic_reply.py"
        with patch("sora_commander_next.approval_v2_owner_local._DIALOG", fixture), patch.dict(
                "os.environ", {"SORA_V2_TEST_CHOICE": "BAD_NONCE"}):
            result = LocalOwnerV2Pilot().review_once_local(sample(), allow_trusted=True)
        self.assertEqual(result.status, "ERROR")
        self.assertIsNone(result.choice)


if __name__ == "__main__":
    unittest.main()
