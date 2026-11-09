"""Promotion regression gate: never broaden AUTO by accident."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sora_commander_next.commands import Command
from sora_commander_next.policy import Policy, Pattern, Grant, Decision

ROOT = str(Path(__file__).resolve().parents[1])
PYTHON = sys.executable

class PromotionGateTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.policy = Policy((ROOT,), (Pattern(PYTHON, ("--version",)),), lambda: self.now)

    def cmd(self, *argv, cwd=ROOT):
        return Command(PYTHON, argv, cwd)

    def test_exact_version_auto_only(self):
        self.assertEqual(self.policy.classify(self.cmd("--version"), "owner"), Decision.AUTO)
        for argv in [("-c", "print(1)"), ("-m", "unittest"), ("--version", "-c", "print(1)"), ("-V",)]:
            with self.subTest(argv=argv):
                self.assertEqual(self.policy.classify(self.cmd(*argv), "owner"), Decision.REVIEW)

    def test_fixed_test_command_requires_grant_and_expires(self):
        req = self.cmd("-m", "unittest", "discover", "-s", "tests", "-p", "test_dev_job_scope_offline.py")
        self.assertEqual(self.policy.classify(req, "owner"), Decision.REVIEW)
        self.policy.add_grant_local(Grant("fixed-test", "owner", ROOT, (Pattern(req.executable, req.argv),), 120.0))
        self.assertEqual(self.policy.classify(req, "owner"), Decision.SCOPED)
        self.assertEqual(self.policy.classify(req, "other-owner"), Decision.REVIEW)
        self.assertEqual(self.policy.classify(self.cmd("-m", "unittest", "discover"), "owner"), Decision.REVIEW)
        self.now = 120.0
        self.assertEqual(self.policy.classify(req, "owner"), Decision.REVIEW)

    def test_grant_revocation_restores_review(self):
        req = self.cmd("-m", "unittest", "discover", "-s", "tests", "-p", "test_dev_job_scope_offline.py")
        self.policy.add_grant_local(Grant("g", "owner", ROOT, (Pattern(req.executable, req.argv),), 120.0))
        self.policy.revoke_local("g", "owner")
        self.assertEqual(self.policy.classify(req, "owner"), Decision.REVIEW)

    def test_outside_root_denied_even_with_auto_pattern(self):
        outside = str(Path(ROOT).parent)
        self.assertEqual(self.policy.classify(self.cmd("--version", cwd=outside), "owner"), Decision.DENY)

if __name__ == "__main__":
    unittest.main()
