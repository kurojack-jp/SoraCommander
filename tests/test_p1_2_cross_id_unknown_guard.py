"""DEV_ONLY process-local UNKNOWN guard. All launch/UI boundaries are fake."""

from dataclasses import asdict, replace
import os
from pathlib import Path
import sys
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.jobs import JobManager, LaunchNotStarted, LaunchOutcomeUnknown
from sora_commander_next.policy import Decision


class CrossIdUnknownGuardTests(unittest.TestCase):
    def setUp(self):
        self.manager = JobManager()
        self.addCleanup(self.manager.close)
        # Every test fakes these boundaries, including tests expecting rejection.
        self.spawn = self.enterContext(patch(
            "sora_commander_next.jobs.subprocess.Popen"))
        self.tree = self.enterContext(patch(
            "sora_commander_next.jobs.WindowsJob", create=True))
        self.thread = self.enterContext(patch(
            "sora_commander_next.jobs.Thread"))
        self.thread.return_value.is_alive.return_value = False
        self.enterContext(patch("sora_commander_next.jobs.os.set_blocking"))
        self.spawn.return_value = SimpleNamespace(
            pid=4242, stdout=Mock(), stderr=Mock(), poll=Mock(return_value=None),
            kill=Mock(), wait=Mock(return_value=0))
        self.request = Command(
            sys.executable, ("-c", "secret-token"), os.getcwd(),
            timeout=5, output_limit=4096, request_id="a" * 32)

    def make_unknown(self, request=None):
        request = request or self.request
        self.spawn.side_effect = OSError("synthetic ambiguous spawn")
        with self.assertRaises(LaunchOutcomeUnknown) as caught:
            self.manager.start(request)
        self.assertEqual(caught.exception.phase, "spawning")
        self.spawn.side_effect = None

    def assert_cross_id_blocked(self, request):
        count = self.spawn.call_count
        trees = self.tree.call_count
        self.assertEqual(self.manager.start(request), {
            "request_id": request.request_id,
            "status": "DENY_DUPLICATE_UNKNOWN",
            "error": "PossibleDuplicateLaunchOutcomeUnknown"})
        self.assertEqual(self.spawn.call_count, count)
        self.assertEqual(self.tree.call_count, trees)

    def make_host(self, decision=Decision.REVIEW, worker=None):
        policy = SimpleNamespace(
            lock=RLock(), classify=Mock(return_value=decision), roots=(),
            generation=1, instance_id="fake-policy")
        approval = Mock()
        approval.create.return_value = SimpleNamespace(
            pending_id="f" * 32, expires_at=1000)
        kwargs = {"worker": worker} if worker is not None else {}
        host = Host(policy=policy, approval=approval, jobs=self.manager, **kwargs)
        host._owner_review_dispatcher = Mock()
        host._owner_review_dispatcher.offer_local.return_value = True
        return host

    def test_spawn_unknown_same_id_and_cross_id_denied_without_spawn(self):
        self.make_unknown()
        self.assertEqual(self.manager.start(self.request), {
            "request_id": self.request.request_id,
            "status": "DENY_DUPLICATE_UNKNOWN", "error": "LaunchOutcomeUnknown"})
        self.assert_cross_id_blocked(replace(self.request, request_id="b" * 32))
        self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual(self.manager.request_status_readonly(self.request.request_id), {
            "request_id": self.request.request_id, "status": "OUTCOME_UNKNOWN",
            "launch_phase": "spawning"})

    def test_timeout_and_output_limit_cannot_bypass(self):
        self.make_unknown()
        for changes in ({"timeout": 6}, {"output_limit": 8192},
                        {"timeout": 7, "output_limit": 16384}):
            with self.subTest(changes=changes):
                self.assert_cross_id_blocked(replace(
                    self.request, request_id="b" * 32, **changes))
        self.assertEqual(len(self.manager._unknown_command_fingerprints), 1)
        digest = next(iter(self.manager._unknown_command_fingerprints))
        self.assertIsInstance(digest, bytes)
        self.assertEqual(len(digest), 32)

    def test_different_argv_cwd_or_executable_remains_launchable(self):
        self.make_unknown()
        variants = (
            {"argv": ("-c", "different")},
            {"cwd": str(Path(self.request.cwd).parent)},
            {"executable": str(Path(sys.executable).with_name("other.exe"))},
            # JSON field/element boundaries must be unambiguous.
            {"argv": ("-csecret-token",)},
        )
        for index, changes in enumerate(variants):
            with self.subTest(changes=changes):
                request = replace(self.request, request_id=str(index) * 32, **changes)
                self.assertFalse(self.manager.blocked_unknown_command_readonly(request))
                self.assertEqual(self.manager.start(request)["status"], "RUNNING")
        self.assertEqual(self.spawn.call_count, 5)

    def test_normal_repeated_commands_and_active_same_id_unchanged(self):
        first = self.manager.start(self.request)
        self.assertEqual(first["status"], "RUNNING")
        reused = self.manager.start(self.request)
        self.assertTrue(reused["reused_session"])
        self.assertEqual(reused["session_id"], first["session_id"])
        second = replace(self.request, request_id="b" * 32)
        self.assertFalse(self.manager.blocked_unknown_command_readonly(second))
        self.assertEqual(self.manager.start(second)["status"], "RUNNING")
        self.assertEqual(self.spawn.call_count, 2)
        self.assertEqual(self.manager._unknown_command_fingerprints, set())

    def test_active_same_id_stays_running_after_other_id_becomes_unknown(self):
        first = self.manager.start(self.request)
        self.make_unknown(replace(self.request, request_id="b" * 32))
        self.assertFalse(self.manager.blocked_unknown_command_readonly(self.request))
        reused = self.manager.start(self.request)
        self.assertTrue(reused["reused_session"])
        self.assertEqual(reused["session_id"], first["session_id"])
        self.assert_cross_id_blocked(replace(self.request, request_id="c" * 32))

    @unittest.skipUnless(os.name == "nt", "Windows pre-spawn failure boundary")
    def test_launch_not_started_never_records_fingerprint(self):
        self.tree.side_effect = OSError("synthetic pre-spawn setup failure")
        with self.assertRaises(LaunchNotStarted) as caught:
            self.manager.start(self.request)
        self.assertEqual(caught.exception.phase, "pre_spawn")
        self.spawn.assert_not_called()
        self.assertEqual(self.manager._unknown_command_fingerprints, set())
        self.assertEqual(self.manager.request_status_readonly(
            self.request.request_id)["status"], "UNKNOWN")
        self.tree.side_effect = None
        second = replace(self.request, request_id="b" * 32)
        self.assertFalse(self.manager.blocked_unknown_command_readonly(second))
        self.assertEqual(self.manager.start(second)["status"], "RUNNING")
        self.assertEqual(self.manager.start(self.request)["status"], "RUNNING")

    @unittest.skipUnless(os.name == "nt", "Windows path case normalization")
    def test_windows_case_and_path_normalization_but_exact_argv(self):
        self.make_unknown()
        equivalent = replace(
            self.request, request_id="b" * 32,
            executable=self.request.executable.swapcase(),
            cwd=os.path.join(self.request.cwd.swapcase(), "."))
        self.assert_cross_id_blocked(equivalent)
        changed_args = replace(equivalent, argv=("-c", "SECRET-TOKEN"))
        self.assertEqual(self.manager.start(changed_args)["status"], "RUNNING")

    def test_cleanup_success_never_resolves_unknown(self):
        self.thread.return_value.start.side_effect = RuntimeError("synthetic monitor loss")
        with self.assertRaises(LaunchOutcomeUnknown):
            self.manager.start(self.request)
        if os.name == "nt":
            self.tree.return_value.close.assert_called_once()
        self.spawn.return_value.kill.assert_called_once()
        self.spawn.return_value.wait.assert_called_once()
        self.assert_cross_id_blocked(replace(self.request, request_id="b" * 32))

    def test_fingerprint_capacity_fails_closed_without_eviction(self):
        with patch("sora_commander_next.jobs._MAX_RETIRED_REQUESTS", 1):
            self.make_unknown()
            # Exercise the digest set's own bound independently of ID tombstones.
            self.manager._uncertain_request_ids.clear()
            different = replace(self.request, request_id="b" * 32, argv=("other",))
            self.assertEqual(self.manager.start(different), {
                "request_id": different.request_id, "status": "DENY_REPLAY_GUARD_FULL",
                "error": "ProtectedBrokerLedgerRequired"})
            self.assert_cross_id_blocked(replace(self.request, request_id="c" * 32))
            self.assertEqual(self.spawn.call_count, 1)
            self.assertEqual(len(self.manager._unknown_command_fingerprints), 1)

    def test_host_review_denied_before_second_pending_or_offer(self):
        host = self.make_host()
        self.assertEqual(host.run_command(**asdict(self.request))["status"], "REVIEW")
        self.make_unknown()
        second = replace(self.request, request_id="b" * 32)
        self.assertEqual(host.run_command(**asdict(second)), {
            "request_id": second.request_id, "status": "DENY_DUPLICATE_UNKNOWN",
            "error": "PossibleDuplicateLaunchOutcomeUnknown"})
        host.approval.create.assert_called_once()
        host._owner_review_dispatcher.offer_local.assert_called_once()
        self.assertEqual(self.spawn.call_count, 1)

    def test_already_pending_review_still_blocked_by_start(self):
        host = self.make_host()
        second = replace(self.request, request_id="b" * 32)
        self.assertEqual(host.run_command(**asdict(second))["status"], "REVIEW")
        self.make_unknown()
        # Simulate the eventual approved launch boundary without Owner UI/ACL.
        self.assert_cross_id_blocked(second)
        host.approval.create.assert_called_once()

    def test_host_policy_deny_precedes_preflight(self):
        host = self.make_host(Decision.DENY)
        with patch.object(self.manager, "blocked_unknown_command_readonly") as preflight:
            self.assertEqual(host.run_command(**asdict(self.request)), {
                "request_id": self.request.request_id, "status": "DENY"})
        preflight.assert_not_called()
        host.approval.create.assert_not_called()
        self.spawn.assert_not_called()

    def test_host_auto_and_custom_worker_preflight(self):
        worker = Mock(return_value={"status": "FAKE_DONE"})
        host = self.make_host(Decision.AUTO, worker=worker)
        for background in (False, True):
            with self.subTest(background=background):
                self.assertEqual(
                    host.run_command(background=background, **asdict(self.request)),
                    {"request_id": self.request.request_id,
                     "status": "DENY_UNPROTECTED_WORKER",
                     "error": "JobManagerRequired"})
        worker.assert_not_called()
        self.spawn.assert_not_called()
        self.make_unknown()
        second = replace(self.request, request_id="b" * 32)
        for background in (False, True):
            with self.subTest(background=background):
                self.assertEqual(host.run_command(background=background, **asdict(second)), {
                    "request_id": second.request_id, "status": "DENY_DUPLICATE_UNKNOWN",
                    "error": "PossibleDuplicateLaunchOutcomeUnknown"})
        worker.assert_not_called()
        host.approval.create.assert_not_called()
        self.assertEqual(self.spawn.call_count, 1)

    def test_preflight_is_readonly_and_start_rechecks_after_clear_preflight(self):
        second = replace(self.request, request_id="b" * 32)
        self.assertFalse(self.manager.blocked_unknown_command_readonly(second))
        self.assertEqual(self.manager._uncertain_request_ids, {})
        self.assertEqual(self.manager._unknown_command_fingerprints, set())
        self.make_unknown()
        for _ in range(2):
            self.assertTrue(self.manager.blocked_unknown_command_readonly(second))
        self.assertNotIn(second.request_id, self.manager._uncertain_request_ids)
        self.assertEqual(self.manager.request_status_readonly(second.request_id)["status"],
                         "UNKNOWN")
        self.assert_cross_id_blocked(second)

    def fail_fingerprint_add(self):
        class Fingerprints(set):
            pass

        fingerprints = Fingerprints()
        fingerprints.add = Mock(side_effect=MemoryError("private-allocation-detail"))
        self.enterContext(patch.object(
            self.manager, "_unknown_command_fingerprints", fingerprints))
        return fingerprints

    def test_add_memory_error_keeps_original_unknown_and_latches_new_ids(self):
        fingerprints = self.fail_fingerprint_add()
        original = OSError("private-spawn-detail secret-token")
        self.spawn.side_effect = original
        with self.assertRaises(LaunchOutcomeUnknown) as caught:
            self.manager.start(self.request)
        self.assertEqual(caught.exception.phase, "spawning")
        self.assertIs(caught.exception.__cause__, original)
        self.assertIs(self.manager._unknown_guard_failed, True)
        self.assertEqual(self.manager._uncertain_request_ids,
                         {self.request.request_id: "spawning"})
        self.assertIs(self.manager.blocked_unknown_command_readonly(self.request), False)
        self.assertEqual(self.manager.start(self.request), {
            "request_id": self.request.request_id,
            "status": "DENY_DUPLICATE_UNKNOWN", "error": "LaunchOutcomeUnknown"})
        counts = self.spawn.call_count, self.tree.call_count
        self.spawn.side_effect = None
        for request_id, argv in (("b", self.request.argv), ("c", ("unrelated",)),
                                 ("d", ("another",)), ("e", self.request.argv)):
            request = replace(self.request, request_id=request_id * 32, argv=argv)
            with self.subTest(request_id=request_id):
                self.assertEqual(self.manager.blocked_unknown_command_readonly(request),
                                 "GUARD_UNAVAILABLE")
                result = self.manager.start(request)
                self.assertEqual(result, {
                    "request_id": request.request_id, "status": "DENY_REPLAY_GUARD_FULL",
                    "error": "ProtectedBrokerLedgerRequired"})
                self.assertNotIn("secret-token", repr(result))
                self.assertNotIn("private-", repr(result))
                self.assertIs(self.manager._unknown_guard_failed, True)
                self.assertNotIn(request.request_id, self.manager._uncertain_request_ids)
        self.assertEqual((self.spawn.call_count, self.tree.call_count), counts)
        self.assertEqual(self.spawn.call_count, 1)
        fingerprints.add.assert_called_once()

    def test_host_memory_error_is_unknown_then_denies_before_review_or_worker(self):
        self.fail_fingerprint_add()
        self.spawn.side_effect = OSError("private-spawn-detail secret-token")
        host = self.make_host(Decision.AUTO)
        result = host.run_command(**asdict(self.request))
        self.assertEqual(result, {
            "request_id": self.request.request_id, "status": "OUTCOME_UNKNOWN",
            "error": "LaunchOutcomeUnknown", "launch_phase": "spawning"})
        self.assertEqual(host.run_command(**asdict(self.request))["status"],
                         "DENY_DUPLICATE_UNKNOWN")
        counts = self.spawn.call_count, self.tree.call_count
        self.spawn.side_effect = None
        for decision in (Decision.REVIEW, Decision.AUTO, Decision.SCOPED, Decision.DENY):
            worker = Mock(return_value={"status": "FAKE_DONE"})
            host = self.make_host(decision, worker=worker)
            for request_id, argv in (("b", self.request.argv), ("c", ("unrelated",))):
                request = replace(self.request, request_id=request_id * 32, argv=argv)
                for background in (False, True):
                    with self.subTest(decision=decision, request_id=request_id,
                                      background=background):
                        result = host.run_command(background=background, **asdict(request))
                        expected = {"request_id": request.request_id, "status": "DENY"}
                        if decision != Decision.DENY:
                            expected.update(status="DENY_REPLAY_GUARD_FULL",
                                            error="ProtectedBrokerLedgerRequired")
                        self.assertEqual(result, expected)
                        self.assertNotIn("secret-token", repr(result))
                        self.assertNotIn("private-", repr(result))
                        self.assertIs(self.manager._unknown_guard_failed, True)
            self.assertEqual(host.approval.mock_calls, [])
            host._owner_review_dispatcher.offer_local.assert_not_called()
            worker.assert_not_called()
        self.assertEqual((self.spawn.call_count, self.tree.call_count), counts)
        self.assertEqual(self.spawn.call_count, 1)

    def test_clean_guard_uses_booleans_and_host_requires_true_identity(self):
        self.assertIs(self.manager._unknown_guard_failed, False)
        self.assertIs(self.manager.blocked_unknown_command_readonly(self.request), False)
        self.make_unknown()
        second = replace(self.request, request_id="b" * 32)
        self.assertIs(self.manager._unknown_guard_failed, False)
        self.assertIs(self.manager.blocked_unknown_command_readonly(second), True)
        unrelated = replace(second, argv=("unrelated",))
        self.assertIs(self.manager.blocked_unknown_command_readonly(unrelated), False)
        host = self.make_host()
        with patch.object(self.manager, "blocked_unknown_command_readonly", return_value=1):
            self.assertEqual(host.run_command(**asdict(unrelated))["status"], "REVIEW")
        host.approval.create.assert_called_once()

    def test_fingerprint_storage_error_latches_guard_but_keeps_unknown(self):
        fingerprints = self.fail_fingerprint_add()
        fingerprints.add.side_effect = RuntimeError("synthetic unrelated failure")
        self.spawn.side_effect = OSError("synthetic ambiguous spawn")
        with self.assertRaises(LaunchOutcomeUnknown) as caught:
            self.manager.start(self.request)
        self.assertEqual(caught.exception.phase, "spawning")
        self.assertIs(self.manager._unknown_guard_failed, True)
        self.assertEqual(self.manager._uncertain_request_ids,
                         {self.request.request_id: "spawning"})
        self.spawn.side_effect = None
        self.assertEqual(self.manager.start(replace(
            self.request, request_id="b" * 32))["status"],
            "DENY_REPLAY_GUARD_FULL")

    @unittest.skipUnless(os.name == "nt", "Windows suspended-child cleanup")
    def test_suspended_child_cleanup_failure_is_unknown_not_safe_retry(self):
        self.tree.return_value.close.side_effect = OSError("cannot close job")
        with patch("sora_commander_next.jobs._Job",
                   side_effect=RuntimeError("before resume setup failed")):
            with self.assertRaises(LaunchOutcomeUnknown) as caught:
                self.manager.start(self.request)
        self.assertEqual(caught.exception.phase, "spawned_suspended")
        self.assertTrue(self.manager._unknown_guard_failed)
        self.assertEqual(self.manager._uncertain_request_ids[self.request.request_id],
                         "spawned_suspended")
        self.assertEqual(self.manager.start(replace(
            self.request, request_id="b" * 32))["status"],
            "DENY_REPLAY_GUARD_FULL")

    @unittest.skipUnless(os.name == "nt", "Windows suspended-child cleanup")
    def test_suspended_child_kill_failure_also_latches_unknown(self):
        self.spawn.return_value.kill.side_effect = OSError("cannot kill")
        with patch("sora_commander_next.jobs._Job",
                   side_effect=RuntimeError("before resume setup failed")):
            with self.assertRaises(LaunchOutcomeUnknown) as caught:
                self.manager.start(self.request)
        self.assertEqual(caught.exception.phase, "spawned_suspended")
        self.assertTrue(self.manager._unknown_guard_failed)
        self.assertEqual(self.manager._uncertain_request_ids[self.request.request_id],
                         "spawned_suspended")
        self.assertEqual(self.manager.start(replace(
            self.request, request_id="b" * 32))["status"],
            "DENY_REPLAY_GUARD_FULL")

    def test_pipe_close_failure_does_not_mask_unknown_or_allow_cross_id(self):
        self.thread.return_value.start.side_effect = RuntimeError("supervisor failed")
        self.spawn.return_value.stdout.close.side_effect = OSError("pipe close failed")
        with self.assertRaises(LaunchOutcomeUnknown) as caught:
            self.manager.start(self.request)
        self.assertEqual(caught.exception.phase, "resumed" if os.name == "nt"
                         else "spawned_running")
        self.assertEqual(self.manager._uncertain_request_ids[self.request.request_id],
                         caught.exception.phase)
        self.spawn.return_value.stderr.close.assert_called_once()
        self.assertTrue(self.manager._unknown_guard_failed)
        denied = self.manager.start(replace(self.request, request_id="b" * 32))
        self.assertEqual(denied["status"], "DENY_REPLAY_GUARD_FULL")
        self.assertEqual(self.spawn.call_count, 1)

    def test_unexpected_cleanup_failure_preserves_unknown_before_cleanup(self):
        self.thread.return_value.start.side_effect = RuntimeError("supervisor failed")
        with patch("sora_commander_next.jobs._discard_spawn",
                   side_effect=RuntimeError("synthetic cleanup failure")):
            with self.assertRaises(LaunchOutcomeUnknown):
                self.manager.start(self.request)
        self.assertIs(self.manager._unknown_guard_failed, True)
        self.assertIn(self.request.request_id, self.manager._uncertain_request_ids)
        self.assertEqual(len(self.manager._unknown_command_fingerprints), 1)
        self.assertEqual(self.manager.start(replace(
            self.request, request_id="b" * 32))["status"],
            "DENY_REPLAY_GUARD_FULL")


if __name__ == "__main__":
    unittest.main()
