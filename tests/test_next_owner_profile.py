"""An explicit local profile can cover development without widening the default."""
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sora_commander_next.stdio_main import owner_pilot_root
from sora_commander_next.policy import Policy, Decision
from sora_commander_next.commands import Command


class OwnerProfileTests(TestCase):
    def test_default_tests_and_explicit_development_scope(self):
        with TemporaryDirectory() as folder:
            repo=Path(folder).resolve()
            tests=repo/"tests"
            tests.mkdir()
            self.assertEqual(owner_pilot_root(repo, "tests"), tests)
            self.assertEqual(owner_pilot_root(repo, "development"), repo)
            self.assertEqual(Policy(roots=(str(tests),)).classify(
                Command(sys.executable, ["--version"], str(repo)), "owner"), Decision.DENY)
            self.assertEqual(Policy(roots=(str(repo),)).classify(
                Command(sys.executable, ["--version"], str(repo)), "owner"), Decision.REVIEW)

    def test_arbitrary_root_or_unknown_profile_cannot_be_selected(self):
        with TemporaryDirectory() as folder:
            root=Path(folder).resolve()
            for profile in ("", "..", "D:/", "production", str(root)):
                with self.subTest(profile=profile), self.assertRaises(ValueError):
                    owner_pilot_root(root, profile)
