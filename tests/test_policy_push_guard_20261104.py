"""The push guard wired into Policy and Host. Nothing runs, and no window can open."""
from pathlib import Path
import sys
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from tests._no_real_dialog import NoRealDialogTestCase
from sora_commander_next import release_inventory as inventory
from sora_commander_next.approval import LocalApproval
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.policy import Decision, Grant, Pattern, Policy

GIT = r"C:\Program Files\Git\cmd\git.exe"
WORK = ("push", "origin", "HEAD:refs/heads/sora/g2-protected-broker-20261028")
MAIN = ("push", "origin", "HEAD:refs/heads/main")


class PolicyTests(NoRealDialogTestCase):
    def setUp(self):
        super().setUp()
        self.now = 1000.0
        self.policy = Policy((str(ROOT),), clock=lambda: self.now)

    def classify(self, argv, executable=GIT):
        return self.policy.classify(Command(executable, tuple(argv), str(ROOT)), "owner")

    def test_main_is_denied_and_the_explicit_branch_form_is_reviewed(self):
        self.assertEqual(self.classify(MAIN), Decision.DENY)
        self.assertEqual(self.classify(("push", "origin", "main")), Decision.DENY)
        self.assertEqual(self.classify(("push", "--no-verify", *WORK[1:])), Decision.DENY)
        self.assertEqual(self.classify(WORK), Decision.REVIEW)

    def test_a_saved_alias_and_a_hook_change_are_denied(self):
        self.assertEqual(self.classify(("p", "origin", "main")), Decision.DENY)
        self.assertEqual(self.classify(("send-pack", "origin", "main")), Decision.DENY)
        self.assertEqual(self.classify(("config", "core.hooksPath", "x")), Decision.DENY)

    def test_configuration_and_program_routes_are_denied(self):
        for argv in (("config", "--edit"), ("config", "set", "include.path", "x"), ("rebase", "--exec", "x.bat"),
                     ("bisect", "run", "x.bat"), ("submodule", "foreach", "x.bat"), ("remote", "add", "o", "x"),
                     ("-C", r"D:\other", "status"), ("-c", "core.fsmonitor=x.bat", "status"),
                     ("log", "--output=.git/config")):
            with self.subTest(argv=argv):
                self.assertEqual(self.classify(argv), Decision.DENY)
        for argv in (("config", "--list"), ("rebase", "--continue"), ("remote", "-v"), ("--no-pager", "log")):
            with self.subTest(argv=argv):
                self.assertEqual(self.classify(argv), Decision.REVIEW)

    def test_other_commands_keep_their_decision(self):
        for argv in (("status",), ("add", "--", "a.txt"), ("commit", "-m", "x", "--", "a.txt"), ("clean", "-n"), ("switch", "x")):
            with self.subTest(argv=argv):
                self.assertEqual(self.classify(argv), Decision.REVIEW)
        self.assertEqual(self.classify(("-c", "print(1)"), sys.executable), Decision.REVIEW)

    def test_requests_that_discard_work_are_denied(self):
        for argv in (("reset", "--hard"), ("clean", "-fdx"), ("checkout", "--", "a"), ("restore", "."), ("switch", "-f", "x"),
                     ("stash",), ("stash", "drop"), ("rm", "-f", "a"), ("rm", "--cached", "a.txt"), ("commit", "-m", "x"), ("commit", "--amend"),
                     ("commit", "-m", "x", "--", "."), ("add", "-A"), ("add", "--", "."), ("apply", "-R", "x.patch"),
                     ("branch", "-D", "x"), ("reflog", "expire"), ("gc",)):
            with self.subTest(argv=argv):
                self.assertEqual(self.classify(argv), Decision.DENY)

    def test_a_grant_cannot_turn_a_discard_into_scoped(self):
        hard = ("reset", "--hard")
        self.policy.add_grant_local(Grant("g2", "owner", str(ROOT), (Pattern(GIT, hard),), self.now + 60))
        self.assertEqual(self.classify(hard), Decision.DENY)

    def test_a_grant_cannot_turn_a_refused_push_into_scoped(self):
        self.policy.add_grant_local(Grant("g1", "owner", str(ROOT), (Pattern(GIT, MAIN), Pattern(GIT, WORK)), self.now + 60))
        self.assertEqual(self.classify(MAIN), Decision.DENY)
        self.assertEqual(self.classify(WORK), Decision.SCOPED)

    def test_auto_cannot_allow_it_either(self):
        policy = Policy((str(ROOT),), auto=(Pattern(GIT, MAIN),), clock=lambda: self.now)
        self.assertEqual(policy.classify(Command(GIT, MAIN, str(ROOT)), "owner"), Decision.DENY)


class HostTests(NoRealDialogTestCase):
    def setUp(self):
        super().setUp()
        self.now = 1000.0
        self.host = Host(Policy((str(ROOT),), clock=lambda: self.now), "owner", LocalApproval(lambda: self.now))
        self.addCleanup(self.host.jobs.close)
        self.dispatcher = Mock()
        self.host._owner_review_dispatcher = self.dispatcher

    def test_a_refused_push_opens_no_review_and_starts_nothing(self):
        result = self.host.run_command(background=True, executable=GIT, argv=list(MAIN), cwd=str(ROOT))
        self.assertEqual(result["status"], "DENY")
        self.assertNotIn("pending_id", result)
        self.dispatcher.offer_local.assert_not_called()
        self.assertEqual(self.host.approval.pending_local(), ())
        self.assertFalse(self.host.jobs._jobs)

    def test_a_discard_opens_no_review_and_starts_nothing(self):
        result = self.host.run_command(background=True, executable=GIT, argv=["reset", "--hard"], cwd=str(ROOT))
        self.assertEqual(result["status"], "DENY")
        self.assertNotIn("pending_id", result)
        self.dispatcher.offer_local.assert_not_called()
        self.assertFalse(self.host.jobs._jobs)

    def test_waiting_call_is_refused_the_same_way(self):
        result = self.host.run_command_wait(executable=GIT, argv=list(MAIN), cwd=str(ROOT), wait_seconds=1)
        self.assertEqual(result["status"], "DENY")
        self.dispatcher.offer_local.assert_not_called()


class InventoryTests(NoRealDialogTestCase):
    def test_guard_ships_with_policy(self):
        self.assertIn("git_push_guard.py", inventory.MODULES)
        self.assertNotIn("git_push_guard.py", inventory.DEV_ONLY_MODULES)


if __name__ == "__main__":
    import unittest
    unittest.main()
