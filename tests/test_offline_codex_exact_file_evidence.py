"""Real bounded exact-file observations with disposable local fixtures only."""
from hashlib import sha256
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
import unittest
from unittest.mock import patch

from sora_commander_next.offline_codex_exact_file_evidence import (
    ExactEvidenceError, observe_exact_files,
)
from sora_commander_next.offline_codex_task_acceptance import (
    RequiredFile, assess_task_evidence, FileEvidence, TestEvidence,
)

TARGET = "src/example.py"
NEW = "src/new.py"
TEST = "test_offline_codex_exact_file_evidence"
A = b"x = 1\r\n"
B = b"x = 2\r\n"


def complete_job():
    return {
        "status": "SUCCESS", "exitCode": 0, "codexStarted": True,
        "evidence": {"turnStarted": True, "turnCompleted": True,
                     "turnFailed": False, "errorEvent": False,
                     "malformedLines": 0},
    }


class ExactFileObservations(unittest.TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
        self.tmp = self.enterContext(inherited_test_temp())
        self.root = Path(self.tmp)
        (self.root / "src").mkdir()
        (self.root / TARGET).write_bytes(A)
        self.requirements = (RequiredFile(TARGET, sha256(A).hexdigest()),)

    _DEFAULT = object()

    def read(self, needs=None, path=_DEFAULT):
        return observe_exact_files(
            str(self.root) if path is self._DEFAULT else path,
            self.requirements if needs is None else needs,
        )

    def test_actual_digest_from_explicit_file_only(self):
        r = self.read()
        self.assertEqual(r, (FileEvidence(TARGET, sha256(A).hexdigest()),))
        (self.root / TARGET).write_bytes(B)
        self.assertEqual(self.read()[0].after_sha256, sha256(B).hexdigest())

    def test_missing_new_target_has_explicit_none(self):
        requirements = (RequiredFile(NEW, None),)
        self.assertEqual(self.read(requirements), (FileEvidence(NEW, None),))

    def test_missing_new_target_does_not_make_task_review_ready(self):
        new_requirements = (RequiredFile(NEW, None),)
        result = assess_task_evidence(
            complete_job(), required_files=new_requirements,
            required_tests=(TEST,),
            observed_files=self.read(new_requirements),
            observed_tests=(TestEvidence(TEST, 0, 1, 0, 0, True),),
        )
        self.assertEqual(result.task_acceptance, "HOLD")
        self.assertEqual(result.reason, "REQUIRED_SOURCE_FILE_ABSENT")

    def test_disappeared_optional_source_still_denied(self):
        optional = (RequiredFile(TARGET, sha256(A).hexdigest(), False),)
        (self.root / TARGET).unlink()
        result = assess_task_evidence(
            complete_job(), required_files=optional, required_tests=(TEST,),
            observed_files=self.read(optional),
            observed_tests=(TestEvidence(TEST, 0, 1, 0, 0, True),),
        )
        self.assertEqual(result.task_acceptance, "HOLD")
        self.assertEqual(result.reason, "REQUIRED_SOURCE_FILE_ABSENT")

    def test_unreadable_or_changed_file_fails_closed(self):
        with patch("sora_commander_next.offline_codex_exact_file_evidence._read_inspection_text",
                   side_effect=PermissionError("owner denied")) as called:
            with self.assertRaises(ExactEvidenceError):
                self.read()
            called.assert_called_once()

    def test_does_not_reinterpret_disappearance_during_read_as_absence(self):
        with patch("sora_commander_next.offline_codex_exact_file_evidence._read_inspection_text",
                   side_effect=FileNotFoundError("raced")):
            with self.assertRaises(ExactEvidenceError):
                self.read()

    def test_missing_parent_is_not_missing_file(self):
        with self.assertRaises(ExactEvidenceError):
            self.read((RequiredFile("not_a_dir/new.py", None),))

    def test_batch_fails_entirely_instead_of_truncating_denied_result(self):
        with patch("sora_commander_next.offline_codex_exact_file_evidence._read_inspection_text",
                   side_effect=PermissionError("blocked")):
            with self.assertRaises(ExactEvidenceError):
                self.read((RequiredFile(NEW, None), *self.requirements))

    def test_no_repo_scan_or_process_launch(self):
        with (patch("pathlib.Path.rglob", side_effect=AssertionError("recursive walk")),
              patch("pathlib.Path.iterdir", side_effect=AssertionError("listing")),
              patch("subprocess.Popen", side_effect=AssertionError("execution"))):
            self.assertEqual(self.read()[0].after_sha256, sha256(A).hexdigest())

    def test_explicit_invalid_root_and_targets(self):
        for root in ("", "relative", None):
            with self.subTest(root=root), self.assertRaises(ExactEvidenceError):
                self.read(path=root)
        for needs in ((), [], (object(),)):
            with self.subTest(needs=needs), self.assertRaises(ExactEvidenceError):
                self.read(needs)

    def test_casefold_duplicate_targets_not_accepted(self):
        with self.assertRaises(ExactEvidenceError):
            self.read((RequiredFile(TARGET, sha256(A).hexdigest()),
                       RequiredFile(TARGET.upper(), sha256(A).hexdigest())))

    def test_symlink_target_refused_if_supported(self):
        alias = self.root / "src" / "linked.py"
        try:
            alias.symlink_to(self.root / TARGET)
            # The inherited fixture refuses links; remove ours first.
            self.addCleanup(alias.unlink)
        except (OSError, NotImplementedError):
            self.skipTest("Windows symlink privilege not available")
        with self.assertRaises(ExactEvidenceError):
            self.read((RequiredFile("src/linked.py", sha256(A).hexdigest()),))

    def test_symlink_parent_refused_if_supported(self):
        other = self.root / "safe"
        other.mkdir()
        (other / "foo.py").write_bytes(A)
        link = self.root / "shortcut"
        try:
            link.symlink_to(other, target_is_directory=True)
            # The inherited fixture refuses links; remove ours first.
            self.addCleanup(link.unlink)
        except (OSError, NotImplementedError):
            self.skipTest("Windows junction/symlink privilege not available")
        with self.assertRaises(ExactEvidenceError):
            self.read((RequiredFile("shortcut/foo.py", sha256(A).hexdigest()),))

    def test_only_bounded_utf8_single_link_source(self):
        (self.root / TARGET).write_bytes(b"\xff")
        with self.assertRaises(ExactEvidenceError):
            self.read()
        (self.root / TARGET).write_bytes(b"x" * 65537)
        with self.assertRaises(ExactEvidenceError):
            self.read()


if __name__ == "__main__":
    unittest.main()
