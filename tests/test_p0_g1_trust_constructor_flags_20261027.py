"""G1 review-only trust flags must remain non-authorizing."""
from dataclasses import replace
import unittest

from sora_commander_next.release_trust_store_plan import (
    REQUIRED_EVIDENCE,
    TrustStoreGate,
    TrustStorePlan,
    evaluate_trust_evidence,
)


PLAN_ARGS = (
    r"C:\Program Files\SoraCommanderNext",
    r"C:\ProgramData\SoraCommanderNext\Trust",
    r"C:\Users\Owner\AppData\Local\SoraCommanderNext",
    "OFFLINE_OWNER_VERIFIED", "PROTECTED_ADMIN_JOURNAL",
)
GATE_FLAGS = (
    "allowed_to_install", "allowed_to_activate", "allowed_to_connect_remote",
)


class TrustConstructorFlagsTests(unittest.TestCase):
    def assert_non_authorizing(self, plan, gate):
        self.assertIs(plan.approved, False)
        for flag in GATE_FLAGS:
            self.assertIs(getattr(gate, flag), False, flag)

    def test_plan_constructor_and_replace_reject_approval(self):
        plan = TrustStorePlan(*PLAN_ARGS)
        self.assertIs(plan.approved, False)
        with self.subTest(operation="sixth_positional_argument"):
            with self.assertRaises(TypeError):
                TrustStorePlan(*PLAN_ARGS, True)
        with self.subTest(operation="replace_approved"):
            with self.assertRaises(TypeError):
                replace(plan, approved=True)

    def test_gate_constructor_and_replace_reject_all_flags(self):
        gate = TrustStoreGate("REVIEW_ONLY")
        self.assertEqual(gate.status, "REVIEW_ONLY")
        for flag in GATE_FLAGS:
            self.assertIs(getattr(gate, flag), False, flag)
            with self.subTest(flag=flag, operation="constructor"):
                with self.assertRaises(TypeError):
                    TrustStoreGate("REVIEW_ONLY", **{flag: True})
            with self.subTest(flag=flag, operation="replace"):
                with self.assertRaises(TypeError):
                    replace(gate, **{flag: True})

    def test_all_true_evidence_remains_review_only(self):
        plan = TrustStorePlan(*PLAN_ARGS)
        gate = evaluate_trust_evidence(
            plan, evidence={key: True for key in REQUIRED_EVIDENCE},
        )
        self.assertEqual(gate.status, "TRUST_DESIGN_REVIEW_ONLY_ALL_CHECKS_PRESENT")
        self.assertEqual(gate.failures, ())
        self.assert_non_authorizing(plan, gate)

    def test_integer_one_is_failed_evidence(self):
        plan = TrustStorePlan(*PLAN_ARGS)
        evidence = {key: True for key in REQUIRED_EVIDENCE}
        evidence[REQUIRED_EVIDENCE[0]] = 1
        gate = evaluate_trust_evidence(plan, evidence=evidence)
        self.assertEqual(gate.status, "TRUST_DESIGN_REVIEW_ONLY_MISSING_EVIDENCE")
        self.assertEqual(gate.failures, (REQUIRED_EVIDENCE[0],))
        self.assert_non_authorizing(plan, gate)


if __name__ == "__main__":
    unittest.main()
