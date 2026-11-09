"""Codex transport success is not an engineering acceptance receipt."""
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from sora_commander_next.offline_codex_task_acceptance import (
    FileEvidence, RequiredFile, TaskReceiptError, TestEvidence,
    assess_task_evidence,
)

BEFORE = "a" * 64
AFTER = "b" * 64
SRC = "src/sora_commander_next/offline_exact_source_scope.py"
TEST = "test_offline_exact_source_scope"


def job(status="SUCCESS", exit_code=0):
    return {
        "status": status,
        "exitCode": exit_code,
        "codexStarted": True,
        "evidence": {
            "turnStarted": True, "turnCompleted": True,
            "turnFailed": False, "errorEvent": False,
            "malformedLines": 0,
        },
    }


def evidence(cases=16, *, code=0, failed=0, errors=0, complete=True):
    return TestEvidence(TEST, code, cases, failed, errors, complete)


def review(j=None, *, files=None, tests=None, blocker=None, required=None):
    return assess_task_evidence(
        job() if j is None else j,
        required_files=(RequiredFile(SRC, BEFORE),) if required is None else required,
        required_tests=(TEST,),
        observed_files=(FileEvidence(SRC, AFTER),) if files is None else files,
        observed_tests=(evidence(),) if tests is None else tests,
        blocker=blocker,
    )


class CodexTaskAcceptanceTests(unittest.TestCase):
    def test_cli_success_without_observed_files_cannot_pass(self):
        result = review(files=(), tests=())
        self.assertEqual(result.cli_lifecycle, "SUCCESS")
        self.assertEqual(result.task_acceptance, "UNVERIFIED")
        self.assertEqual(result.reason, "POST_JOB_FILE_EVIDENCE_MISSING")

    def test_real_previous_job_success_but_human_observed_hold(self):
        # Corresponds to b160bf...: Codex JSONL turn completed while its
        # final answer stopped at Permission denied; no edits or tests.
        result = review(files=(FileEvidence(SRC, BEFORE),),
                        tests=(), blocker="ACCESS_DENIED")
        self.assertEqual(result.cli_lifecycle, "SUCCESS")
        self.assertEqual(result.task_acceptance, "HOLD")
        self.assertEqual(result.reason, "ACCESS_DENIED")
        self.assertFalse(result.release_permitted)
        self.assertFalse(result.execution_permitted)

    def test_unchanged_required_file_is_hold_even_if_codex_says_success(self):
        result = review(files=(FileEvidence(SRC, BEFORE),))
        self.assertEqual(result.task_acceptance, "HOLD")
        self.assertEqual(result.reason, "REQUIRED_SOURCE_CHANGE_ABSENT")

    def test_lifecycle_failure_is_not_task_success(self):
        result = review(job("FAILED", 1))
        self.assertEqual(result.cli_lifecycle, "FAILED")
        self.assertEqual(result.task_acceptance, "HOLD")

    def test_unknown_lifecycle_is_unverified_even_with_fake_perfect_evidence(self):
        for status, code in (("UNKNOWN", 3), ("RUNNING", None),
                             ("STARTING", None), ("SUCCESS", 1)):
            with self.subTest(status=status, code=code):
                result = review(job(status, code))
                self.assertEqual(result.cli_lifecycle, "UNKNOWN")
                self.assertEqual(result.task_acceptance, "UNVERIFIED")

    def test_inconsistent_success_events_do_not_promote(self):
        for key, value in (("turnCompleted", False), ("turnFailed", True),
                           ("turnStarted", False), ("errorEvent", True),
                           ("malformedLines", 2)):
            j = job()
            j["evidence"][key] = value
            with self.subTest(key=key):
                self.assertEqual(review(j).task_acceptance, "UNVERIFIED")

    def test_missing_required_tests_are_not_implicitly_ok(self):
        result = review(tests=())
        self.assertEqual(result.cli_lifecycle, "SUCCESS")
        self.assertEqual(result.reason, "REQUIRED_TEST_EVIDENCE_MISSING")
        self.assertEqual(result.task_acceptance, "UNVERIFIED")
        self.assertEqual(result.observed_changes, 1)

    def test_test_failed_or_incomplete(self):
        for t, want, reason in (
            (evidence(code=1), "HOLD", "REQUIRED_TEST_FAILED"),
            (evidence(failed=1), "HOLD", "REQUIRED_TEST_FAILED"),
            (evidence(errors=1), "HOLD", "REQUIRED_TEST_FAILED"),
            (evidence(cases=0), "UNVERIFIED", "TEST_OUTPUT_INCOMPLETE"),
            (evidence(complete=False), "UNVERIFIED", "TEST_OUTPUT_INCOMPLETE"),
        ):
            with self.subTest(t=t):
                result = review(tests=(t,))
                self.assertEqual((result.task_acceptance, result.reason), (want, reason))

    def test_complete_claim_remains_review_ready_and_never_pass(self):
        result = review()
        self.assertEqual(result.cli_lifecycle, "SUCCESS")
        self.assertEqual(result.task_acceptance, "REVIEW_READY")
        self.assertEqual(result.reason, "EXTERNAL_EVIDENCE_VERIFICATION_REQUIRED")
        self.assertEqual((result.observed_changes, result.checked_tests), (1, 1))
        self.assertEqual(result.authority, "NONE_REVIEW_ONLY")
        with self.assertRaises(FrozenInstanceError):
            result.task_acceptance = "PASS"

    def test_creation_collision_and_missing_source_cannot_pass(self):
        created = (RequiredFile("docs/new.md", None),)
        result = review(files=(FileEvidence("docs/new.md", None),), required=created)
        self.assertEqual(result.task_acceptance, "HOLD")
        self.assertEqual(result.reason, "REQUIRED_SOURCE_FILE_ABSENT")

    def test_forbid_bad_paths_and_aliases(self):
        paths = (
            "/root.py", "../src/file.py", "src//file.py",
            "src/./file.py", "src/../file.py",
            "src/.git/secrets.py", "src/.venv/lib.py",
            "src/CON.txt", "src/COM1.py", "src/file.py.",
            "src/README~1.py", "src\\file.py", "D:/file.py",
            "src/file.py:alternate", "src/file.py" + "\0",
        )
        for path in paths:
            with self.subTest(path=repr(path)), self.assertRaises(TaskReceiptError):
                RequiredFile(path, BEFORE)

    def test_duplicate_casefold_paths_rejected(self):
        required = (RequiredFile(SRC, BEFORE),
                    RequiredFile(SRC.upper(), BEFORE))
        with self.assertRaises(TaskReceiptError):
            review(required=required)

    def test_duplicate_or_forged_out_of_scope_evidence_rejected(self):
        for files in (
            (FileEvidence(SRC, AFTER), FileEvidence(SRC, AFTER)),
            (FileEvidence("src/foreign.py", AFTER),),
        ):
            with self.subTest(files=files), self.assertRaises(TaskReceiptError):
                review(files=files)

    def test_reject_unrecognized_blocker_or_unexpected_test(self):
        with self.assertRaises(TaskReceiptError):
            review(blocker="everything succeeded")
        with self.assertRaises(TaskReceiptError):
            review(tests=(TestEvidence("test_unknown", 0, 1, 0, 0, True),))

    def test_explicit_type_and_digest_validation(self):
        for digest in ("A"*64, "x"*64, "", True, 123):
            with self.subTest(digest=digest), self.assertRaises(TaskReceiptError):
                RequiredFile(SRC, digest)
        with self.assertRaises(TaskReceiptError):
            TestEvidence(TEST, True, 1, 0, 0, True)
        with self.assertRaises(TaskReceiptError):
            TestEvidence(TEST, 0, 1, 2, 0, True)
        with self.assertRaises(TaskReceiptError):
            review(j={"status": "SUCCESS"})
        with self.assertRaises(TaskReceiptError):
            review(j=job(), tests=[evidence()])

    def test_candidate_never_touches_disks_or_runs_processes(self):
        with (patch("pathlib.Path.open", side_effect=AssertionError("unapproved file IO")),
              patch("subprocess.Popen", side_effect=AssertionError("unapproved spawn"))):
            self.assertEqual(review().task_acceptance, "REVIEW_READY")


if __name__ == "__main__":
    unittest.main()
