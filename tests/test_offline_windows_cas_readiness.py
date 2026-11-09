"""Pure explicit CAS readiness matrix, never operates on a user file."""
import unittest
from sora_commander_next.offline_windows_cas_readiness import WindowsCasReadiness
from sora_commander_next.source_review import SourceConflict

FIELDS = tuple(WindowsCasReadiness.__dataclass_fields__)
class WindowsCasReadinessTests(unittest.TestCase):
    def make(self, **changes):
        row={key:True for key in FIELDS}
        row.update(changes)
        return WindowsCasReadiness(**row)
    def test_each_missing_requirement_holds(self):
        for key in FIELDS:
            with self.subTest(key=key):
                result=self.make(**{key:False}).assess()
                self.assertEqual(result["status"],"HOLD")
                self.assertEqual(result["missing"],(key,))
                self.assertFalse(result["write_authorized"])
    def test_all_true_still_requires_review_not_permission(self):
        result=self.make().assess()
        self.assertEqual(result["status"],"REVIEW_REQUIRED")
        self.assertFalse(result["write_authorized"])
        with self.assertRaisesRegex(SourceConflict,"STRONG_SOURCE_CAS_NOT_ACCEPTED"):
            self.make().commit()
    def test_non_bool_never_counts_as_verified(self):
        for value in (1, None, "yes"):
            with self.subTest(value=value),self.assertRaises(SourceConflict):
                self.make(authenticated_broker=value).assess()
