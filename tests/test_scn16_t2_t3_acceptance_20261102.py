"""SCN16: real harmless child launch and synthetic approval-monitor race tests."""
from dataclasses import replace
from pathlib import Path
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.jobs import _child_environment
from sora_commander_next.approval import LocalApproval
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.windows_owner import WindowsOwnerIdentity
from sora_commander_next.windows_owner_controller import WindowsOwnerController


class ChildExecutionCompatibility(unittest.TestCase):
    def test_python_executable_with_restricted_real_environment(self):
        env = _child_environment()
        env.pop("CONTROL_PLANE_API_KEY", None)
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c",
             "import os,sys;assert sys.version_info.major==3;"
             "assert 'CONTROL_PLANE_API_KEY' not in os.environ;"
             "print('CHILD_PYTHON_PASS')"],
            env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CHILD_PYTHON_PASS", result.stdout)

    def test_git_executable_with_restricted_real_environment(self):
        import shutil
        git = shutil.which("git")
        if git is None:
            self.skipTest("git executable unavailable")
        result = subprocess.run(
            [git, "--version"], env=_child_environment(), cwd=str(ROOT),
            capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("git version "))


class ApprovalMonitorRace(unittest.TestCase):
    def setUp(self):
        self.now = 20.0
        self.valid = WindowsOwnerIdentity("S-1-5-21-99-1001", 1, (12, 5), True)
        self.current = self.valid
        self.probe = patch("sora_commander_next.windows_owner.current_windows_owner",
                           side_effect=lambda: self.current)
        self.probe.start()
        self.addCleanup(self.probe.stop)
        self.host = Host(Policy((str(ROOT),), clock=lambda: self.now),
                         self.valid.sid, LocalApproval(lambda: self.now))
        self.addCleanup(self.host.jobs.close)
        self.ctrl = WindowsOwnerController(self.host)
        # T3 never launches a command or GUI, including on a regression.
        self.start = self.enterContext(patch.object(
            self.host.jobs, 'start', return_value={'status': 'RUNNING', 'session_id': 'mock'}))
        self.gui = self.enterContext(patch(
            'sora_commander_next.windows_owner_controller._authorize_interactive_local',
            side_effect=AssertionError('unexpected GUI')))

    def make_pending(self):
        cmd = Command(sys.executable, ("-I", "-S", "-c", "print('synthetic')"), str(ROOT))
        response = self.host.run_command(background=True, executable=cmd.executable,
                                          argv=list(cmd.argv), cwd=cmd.cwd)
        self.assertEqual(response["status"], "REVIEW")
        return response["pending_id"]

    def run_mutation(self, mutate):
        pending = self.make_pending()
        locks_held = []
        failures, acquired = [], threading.Event()
        policy = self.host.policy
        def worker():
            try:
                with policy.lock:
                    acquired.set()
                    mutate()
            except BaseException as exc:
                failures.append(exc)
        thread = threading.Thread(target=worker, daemon=True)
        def monitor(_request):
            probe = getattr(policy.lock, "_is_owned", None)
            locks_held.append(probe() if callable(probe) else None)
            thread.start()
            # The fake monitor blocks until a different thread has acquired
            # policy.lock and completed its mutation. No timing race/sleep.
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'monitor retained policy lock')
            self.assertTrue(acquired.is_set())
        try:
            with patch("sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job",
                       side_effect=monitor):
                result = self.ctrl.approve_local(pending)
        finally:
            if thread.ident is not None:
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(locks_held, [False])
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["launch_outcome"], "NOT_STARTED")
        self.assertEqual(result['approval_state'], 'CONSUMED')
        self.start.assert_not_called()
        self.gui.assert_not_called()
        self.assertEqual(len(self.host.jobs._jobs), 0)
        self.assertNotIn(pending, self.host.approval._pending)
        # Same consumed ID cannot replay, even if the cause was transient.
        self.assertEqual(self.ctrl.approve_local(pending)['status'], 'ERROR')
        self.start.assert_not_called()
        return locks_held

    def test_expiry_during_monitor_denies_without_launch(self):
        locks = self.run_mutation(lambda: setattr(self, "now", self.now + 65))
        self.assertEqual(locks, [False])  # Monitor runs outside policy.lock.

    def test_policy_change_during_monitor_denies_without_launch(self):
        locks = self.run_mutation(lambda: setattr(
            self.host.policy, "generation", self.host.policy.generation + 1))
        self.assertEqual(locks, [False])

    def test_session_change_during_monitor_denies_without_launch(self):
        locks = self.run_mutation(lambda: setattr(
            self, "current", replace(self.valid, active=False)))
        self.assertEqual(locks, [False])

    def test_expiry_at_exact_deadline_denies_without_launch(self):
        self.run_mutation(lambda: setattr(self, 'now', 80.0))

    def test_owner_sid_during_monitor_denies_without_launch(self):
        self.run_mutation(lambda: setattr(self, 'current', replace(
            self.valid, sid='S-1-5-21-99-2002')))

    def test_owner_logon_during_monitor_denies_without_launch(self):
        self.run_mutation(lambda: setattr(self, 'current', replace(
            self.valid, logon_luid=(13, 5))))

    def test_owner_session_id_during_monitor_denies_without_launch(self):
        self.run_mutation(lambda: setattr(self, 'current', replace(
            self.valid, session_id=2)))

    def test_reentrant_policy_mutation_still_review_denies(self):
        # Public mutator reacquires the RLock and increments generation even
        # without grants. Classification remains REVIEW: equality is essential.
        self.run_mutation(lambda: self.host.policy.revoke_owner_grants_local(self.valid.sid))

    def test_classification_change_during_monitor_denies(self):
        self.run_mutation(lambda: setattr(self.host.policy, 'roots', ()))

    def test_policy_replacement_during_monitor_denies(self):
        self.run_mutation(lambda: setattr(self.host, 'policy', Policy(
            (str(ROOT),), clock=lambda: self.now)))

    def test_approval_replacement_during_monitor_denies(self):
        self.run_mutation(lambda: setattr(self.host, 'approval', LocalApproval(lambda: self.now)))

    def test_success_is_single_use_without_actual_launch(self):
        pending = self.make_pending()
        with patch('sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job') as monitor:
            self.assertEqual(self.ctrl.approve_local(pending)['status'], 'RUNNING')
            self.assertEqual(self.ctrl.approve_local(pending)['status'], 'ERROR')
        monitor.assert_called_once()
        self.start.assert_called_once()
        self.gui.assert_not_called()

    def test_ambiguous_dispatch_never_replayed(self):
        pending = self.make_pending()
        self.start.side_effect = RuntimeError('synthetic ambiguous start')
        with patch('sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job'):
            result = self.ctrl.approve_local(pending)
            self.assertEqual(result['status'], 'OUTCOME_UNKNOWN')
            self.assertEqual(result['launch_outcome'], 'UNKNOWN')
            self.assertEqual(self.ctrl.approve_local(pending)['status'], 'ERROR')
        self.start.assert_called_once()

    def test_separate_thread_policy_mutation_during_monitor_denies(self):
        self.run_mutation(lambda: self.host.policy.revoke_owner_grants_local(self.valid.sid))

    def test_owner_binding_change_during_monitor_denies(self):
        self.run_mutation(lambda: setattr(self.host, 'owner', 'replacement-owner'))

    def test_binding_drift_during_monitor_denies(self):
        # Fault injection only: production Pending and Command are frozen.
        def corrupt():
            record = next(iter(self.host.approval._recent.values()))[0]
            object.__setattr__(record.request, 'argv', ('changed-synthetic-binding',))
        self.run_mutation(corrupt)

    def test_classification_only_change_during_monitor_denies(self):
        from sora_commander_next.policy import Decision
        self.run_mutation(lambda: self.enterContext(patch.object(
            self.host.policy, 'classify', return_value=Decision.DENY)))

    def test_classification_revokes_context_during_revalidation_denies(self):
        policy = self.host.policy
        classify = policy.classify
        def revoke(request, owner):
            decision = classify(request, owner)
            policy.revoke_owner_grants_local(owner)
            return decision
        self.run_mutation(lambda: self.enterContext(patch.object(
            policy, 'classify', side_effect=revoke)))

    def test_dispatch_retains_policy_and_manager_locks(self):
        pending, acquired = self.make_pending(), []
        def dispatch(_request):
            def probe():
                for lock in (self.host.policy.lock, self.host.jobs._lock):
                    ok = lock.acquire(blocking=False)
                    acquired.append(ok)
                    if ok:
                        lock.release()
            thread = threading.Thread(target=probe, daemon=True)
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            return {'status': 'RUNNING', 'session_id': 'mock'}
        self.start.side_effect = dispatch
        with patch('sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job'):
            self.assertEqual(self.ctrl.approve_local(pending)['status'], 'RUNNING')
        self.assertEqual(acquired, [False, False])
        self.start.assert_called_once()

    def test_owner_verification_failure_after_monitor_spends_authority(self):
        self.run_mutation(lambda: self.enterContext(patch.object(
            self.ctrl, '_verify', side_effect=PermissionError('synthetic unavailable'))))

    def test_monitor_exception_spends_authority_without_dispatch(self):
        pending = self.make_pending()
        with patch('sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job',
                   side_effect=RuntimeError('synthetic monitor failure')) as monitor:
            result = self.ctrl.approve_local(pending)
            self.assertEqual(result['status'], 'ERROR')
            self.assertEqual(result['approval_state'], 'CONSUMED')
            self.assertEqual(result['launch_outcome'], 'NOT_STARTED')
            self.assertEqual(self.ctrl.approve_local(pending)['status'], 'ERROR')
        monitor.assert_called_once()
        self.start.assert_not_called()
        self.gui.assert_not_called()


if __name__ == "__main__":
    unittest.main()
