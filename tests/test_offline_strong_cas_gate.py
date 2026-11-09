"""DEV-only denial acceptance: no dry-run can become a write permit."""
import unittest
from hashlib import sha256
from sora_commander_next.offline_source_patch_candidate import inspect_patch_bytes
import test_source_handle_verifier_candidate as handle_tests
from sora_commander_next.source_review import SourceConflict

class StrongCasGateTests(unittest.TestCase):
    def test_digest_match_and_handle_match_still_cannot_commit(self):
        fixture=handle_tests.SourceHandleVerifierCandidateTests(methodName="test_complete_dry_run_matches_but_commit_stays_denied")
        fixture.setUp()
        before=fixture.old
        after=fixture.new
        candidate=inspect_patch_bytes(before,after,sha256(before).hexdigest())
        self.assertEqual(candidate.new_sha256,sha256(after).hexdigest())
        self.assertEqual(candidate.authority,"NONE_PREVIEW_ONLY")
        verified=fixture.verify()
        self.assertEqual(verified.status,"DATA_DRY_RUN_MATCH")
        self.assertEqual(verified.authority,"NONE_DATA_ONLY")
        self.assertEqual(verified.commit,"DENIED_NATIVE_EXCLUSION_NOT_ACCEPTED")
        with self.assertRaisesRegex(SourceConflict,"STRONG_SOURCE_CAS_NOT_ACCEPTED"):
            fixture.verifier.commit(fixture.path,fixture.before,after,lambda: True)
        self.assertEqual(fixture.backend.closed,[4,3,2,1])
        self.assertEqual(fixture.backend.data,before)
    def test_failed_digest_does_not_reach_backend(self):
        fixture=handle_tests.SourceHandleVerifierCandidateTests(methodName="test_complete_dry_run_matches_but_commit_stays_denied")
        fixture.setUp()
        with self.assertRaises(ValueError):
            inspect_patch_bytes(fixture.old,fixture.new,"0"*64)
        self.assertEqual(fixture.backend.opened,[])
        self.assertEqual(fixture.backend.closed,[])
