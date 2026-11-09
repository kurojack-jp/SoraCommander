"""Owner job launch outcomes: launch state vs result certainty.

Mock-only tests. Never spawn a real command: Popen, Thread and the
Windows Job Object are faked. The Windows suspended/resumed paths are
simulated on Linux by injecting a fake WindowsJob; Sora's Windows
regression must run this file on Windows too, where the real
CREATE_SUSPENDED path applies.

Windows rule: every test that mocks Popen MUST also fake WindowsJob.
On Windows, JobManager.start() would otherwise pass the fake child's
pid to the real attach_and_resume (OpenProcess + assign + resume),
which either fails confusingly or, worse, touches a stranger process
that happens to reuse the fake pid. Patching the class is a no-op on
Linux (start() never constructs it there) and a safety net on Windows.
The supervision-failure test is platform-aware: the same failure is
observed after resume on Windows and right after spawn on Linux.
"""
import os
import sys
import types
import unittest
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock, patch

if os.name != "nt":
    # Both modules refuse to import off Windows. The controller tests
    # below build the controller without __init__ and inject _guard
    # directly, so placeholder modules are sufficient here.
    _wo = types.ModuleType("sora_commander_next.windows_owner")

    class _StubGuard:
        sid = "owner-sid"

        def verify(self):
            pass

    _wo.OwnerSessionGuard = _StubGuard
    sys.modules.setdefault("sora_commander_next.windows_owner", _wo)
    _wse = types.ModuleType("sora_commander_next.windows_session_events")

    class _StubLatch:
        def verify(self):
            pass

    _wse.OwnerSessionEventLatch = _StubLatch
    sys.modules.setdefault("sora_commander_next.windows_session_events", _wse)

from sora_commander_next.commands import Command
from sora_commander_next.jobs import (
    JobManager, LaunchNotStarted, LaunchOutcomeUnknown, UnknownSession)
from sora_commander_next.policy import Decision
from sora_commander_next.windows_owner_controller import WindowsOwnerController
from sora_commander_next.approval import binding


class FakeChild:
    """Minimal Popen stand-in: never executes anything.

    stdout/stderr are real (unwritten) pipe files so set_blocking and
    close behave like real Popen pipes; nothing is ever written to them.
    """

    def __init__(self):
        self._readers = []
        self.stdout = self._pipe()
        self.stderr = self._pipe()
        self.pid = 4242
        self._poll = None

    def _pipe(self):
        r, w = os.pipe()
        self._readers.append(r)
        return os.fdopen(w, "wb", buffering=0)

    def poll(self):
        return self._poll

    def kill(self):
        self._poll = -9

    def wait(self, timeout=None):
        return self._poll

    def close_pipes(self):
        for pipe in (self.stdout, self.stderr):
            try:
                pipe.close()
            except OSError:
                pass
        for r in self._readers:
            try:
                os.close(r)
            except OSError:
                pass


class FakeThread:
    """Supervisor thread stand-in: start succeeds, never supervises."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


class BrokenThread(FakeThread):
    def start(self):
        raise RuntimeError("simulated monitor start failure")


class FakeWindowsJob:
    """Windows Job Object stand-in: attach/resume/close are recorded."""

    def __init__(self):
        self.attached = False
        self.closed = False

    def attach_and_resume(self, child):
        self.attached = True

    def terminate(self):
        pass

    def close(self):
        self.closed = True


class FaultyDict(dict):
    """Session index that loses the acceptance write (ACK loss)."""

    def __setitem__(self, key, value):
        raise RuntimeError("session index lost")


class LaunchOutcomeManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = JobManager(max_concurrent=4, capacity=64, retention=300)
        self.addCleanup(self.manager.close)
        self.cwd = os.getcwd()
        self._children = []
        self.addCleanup(self._close_children)

    def make_command(self, request_id):
        return Command(sys.executable, ("-c", "pass"), self.cwd,
                       timeout=5, output_limit=4096, request_id=request_id)

    def make_child(self):
        child = FakeChild()
        self._children.append(child)
        return child

    def _close_children(self):
        for child in self._children:
            child.close_pipes()

    def assert_blocked_resend(self, req, expected_status):
        # A regression in request-id replay protection must fail this test
        # before it can launch a real process or touch a real Windows Job.
        with (patch("sora_commander_next.jobs.WindowsJob",
                    side_effect=AssertionError("rejected resend created Job"),
                    create=True) as tree,
              patch("sora_commander_next.jobs.subprocess.Popen",
                    side_effect=AssertionError("rejected resend spawned child")) as spawn):
            self.assertEqual(self.manager.start(req)["status"], expected_status)
            tree.assert_not_called()
            spawn.assert_not_called()

    def test_spawn_failure_is_unknown_not_auth_error(self):
        req = self.make_command("spawn-failed")
        with (patch("sora_commander_next.jobs.WindowsJob",
                    FakeWindowsJob, create=True),
              patch("sora_commander_next.jobs.subprocess.Popen",
                    side_effect=OSError("ambiguous OS spawn")) as spawn):
            with self.assertRaises(LaunchOutcomeUnknown) as caught:
                self.manager.start(req)
            self.assertEqual(caught.exception.request_id, "spawn-failed")
            self.assertEqual(caught.exception.phase, "spawning")
            self.assertIsInstance(caught.exception.__cause__, OSError)
            # No automatic retry: the same id is refused, Popen not called again.
            self.assert_blocked_resend(req, "DENY_DUPLICATE_UNKNOWN")
            self.assertEqual(spawn.call_count, 1)
        status = self.manager.request_status_readonly("spawn-failed")
        self.assertEqual(status["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(status["launch_phase"], "spawning")

    def test_supervision_start_failure_after_spawn_is_unknown(self):
        req = self.make_command("supervision-failed")
        fake_tree = FakeWindowsJob()
        # On Windows the child runs under a (fake) Job Object, so the
        # failure is observed after resume; on Linux there is no
        # suspension and it is observed right after spawn.
        expected_phase = "resumed" if os.name == "nt" else "spawned_running"
        with (patch("sora_commander_next.jobs.WindowsJob",
                    return_value=fake_tree, create=True),
              patch("sora_commander_next.jobs.subprocess.Popen",
                    return_value=self.make_child()),
              patch("sora_commander_next.jobs.Thread", BrokenThread)):
            with self.assertRaises(LaunchOutcomeUnknown) as caught:
                self.manager.start(req)
            self.assertEqual(caught.exception.phase, expected_phase)
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        if os.name == "nt":
            self.assertTrue(fake_tree.attached)
        self.assert_blocked_resend(req, "DENY_DUPLICATE_UNKNOWN")
        status = self.manager.request_status_readonly("supervision-failed")
        self.assertEqual(status["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(status["launch_phase"], expected_phase)

    def test_windows_resume_then_supervision_failure_is_unknown(self):
        req = self.make_command("resume-failed")
        fake_tree = FakeWindowsJob()
        with (patch.object(os, "name", "nt"),
              patch("sora_commander_next.jobs.WindowsJob",
                    return_value=fake_tree, create=True),
              patch("sora_commander_next.jobs.subprocess.Popen",
                    return_value=self.make_child()),
              patch("sora_commander_next.jobs.Thread", BrokenThread)):
            with self.assertRaises(LaunchOutcomeUnknown) as caught:
                self.manager.start(req)
            self.assertEqual(caught.exception.phase, "resumed")
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertTrue(fake_tree.attached)
        self.assert_blocked_resend(req, "DENY_DUPLICATE_UNKNOWN")
        status = self.manager.request_status_readonly("resume-failed")
        self.assertEqual(status["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(status["launch_phase"], "resumed")

    def test_windows_pre_resume_journal_failure_is_not_started(self):
        req = self.make_command("pre-resume-failed")
        fake_tree = FakeWindowsJob()
        spawned = []

        def fake_popen(*args, **kwargs):
            child = self.make_child()
            spawned.append(child)
            return child

        windows = (patch.object(os, "name", "nt"),
                   patch("sora_commander_next.jobs.WindowsJob",
                         return_value=fake_tree, create=True),
                   patch("sora_commander_next.jobs.Thread", FakeThread))
        # _snapshot_journal only runs with a journal path configured.
        self.manager.journal_path = "/tmp/dummy-journal"
        with (windows[0], windows[1],
              patch("sora_commander_next.jobs.subprocess.Popen",
                    side_effect=fake_popen),
              windows[2],
              patch.object(JobManager, "_snapshot_journal",
                           side_effect=OSError("journal write failed"))):
            with self.assertRaises(LaunchNotStarted) as caught:
                self.manager.start(req)
            self.assertEqual(caught.exception.phase, "spawned_suspended")
            self.assertIsInstance(caught.exception.__cause__, OSError)
        # The child never ran, so resume never happened and the id is free.
        self.assertFalse(fake_tree.attached)
        self.assertTrue(fake_tree.closed)
        self.assertEqual(self.manager.request_status_readonly(
            "pre-resume-failed")["status"], "UNKNOWN")
        # Back to no journal so the resend uses the no-op snapshot path.
        self.manager.journal_path = None
        # A resend is a first execution, not a replay.
        with (patch.object(os, "name", "nt"),
              patch("sora_commander_next.jobs.WindowsJob",
                    return_value=fake_tree, create=True),
              patch("sora_commander_next.jobs.subprocess.Popen",
                    side_effect=fake_popen),
              patch("sora_commander_next.jobs.Thread", FakeThread)):
            reply = self.manager.start(req)
        self.assertEqual(reply["status"], "RUNNING")
        self.assertEqual(len(spawned), 2)

    def test_acceptance_loss_after_supervision_is_unknown(self):
        req = self.make_command("ack-lost")
        self.manager._request_sessions = FaultyDict()
        with (patch("sora_commander_next.jobs.WindowsJob",
                    FakeWindowsJob, create=True),
              patch("sora_commander_next.jobs.subprocess.Popen",
                    return_value=self.make_child()),
              patch("sora_commander_next.jobs.Thread", FakeThread)):
            with self.assertRaises(LaunchOutcomeUnknown) as caught:
                self.manager.start(req)
            self.assertEqual(caught.exception.phase, "supervised")
        # Supervision had started, but the caller never got an acceptance:
        # still UNKNOWN, still no resend.
        self.assert_blocked_resend(req, "DENY_DUPLICATE_UNKNOWN")
        status = self.manager.request_status_readonly("ack-lost")
        self.assertEqual(status["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(status["launch_phase"], "supervised")
        self.assertEqual(self.manager.list_sessions()["count"], 0)

    def test_successful_launch_confirms_supervision(self):
        req = self.make_command("normal-launch")
        with (patch("sora_commander_next.jobs.WindowsJob",
                    FakeWindowsJob, create=True),
              patch("sora_commander_next.jobs.subprocess.Popen",
                    return_value=self.make_child()),
              patch("sora_commander_next.jobs.Thread", FakeThread)):
            first = self.manager.start(req)
            self.assertEqual(first["status"], "RUNNING")
            second = self.manager.start(req)
            self.assertEqual(second["status"], "RUNNING")
            self.assertTrue(second["reused_session"])
            self.assertEqual(second["session_id"], first["session_id"])
        status = self.manager.request_status_readonly("normal-launch")
        self.assertEqual(status["status"], "RUNNING")
        self.assertEqual(status["session_id"], first["session_id"])

    def test_result_retrieval_anomalies(self):
        with self.assertRaises(UnknownSession):
            self.manager.read("0" * 48)
        with self.assertRaises(UnknownSession):
            self.manager.wait_for("0" * 48, 0)
        # A wait timeout is "not yet observed", not "lost": the job stays
        # tracked and readable afterwards.
        req = Command(sys.executable, ("-c", "pass"), self.cwd,
                      timeout=0.01, output_limit=4096,
                      request_id="slow-launch")
        with (patch("sora_commander_next.jobs.WindowsJob",
                    FakeWindowsJob, create=True),
              patch("sora_commander_next.jobs.subprocess.Popen",
                    return_value=self.make_child()),
              patch("sora_commander_next.jobs.Thread", FakeThread)):
            started = self.manager.start(req)
        # wait() uses the request timeout + 2 seconds; keep the bound short.
        timed_out = self.manager.wait(started["session_id"])
        self.assertEqual(timed_out["status"], "OUTCOME_UNKNOWN")
        snapshot = self.manager.wait_for(started["session_id"], 0)
        self.assertEqual(snapshot["status"], "RUNNING")
        self.assertEqual(snapshot["session_id"], started["session_id"])

    def test_retired_status_stays_unknown_but_resend_differs(self):
        req = self.make_command("retired-launch")
        with (patch("sora_commander_next.jobs.WindowsJob",
                    FakeWindowsJob, create=True),
              patch("sora_commander_next.jobs.subprocess.Popen",
                    return_value=self.make_child()),
              patch("sora_commander_next.jobs.Thread", FakeThread)):
            started = self.manager.start(req)
        job = self.manager._jobs[started["session_id"]]
        job.child._poll = 0
        job.done.set()
        job.finished_at = self.manager.clock() - 1000
        self.assertEqual(self.manager.list_sessions()["count"], 0)
        # Pinned behavior: an expired record reports OUTCOME_UNKNOWN
        # (observing an expired job does not imply it never ran).
        self.assertEqual(self.manager.request_status_readonly(
            "retired-launch")["status"], "OUTCOME_UNKNOWN")
        self.assert_blocked_resend(req, "DENY_DUPLICATE_EXPIRED")


class FakeGuard:
    sid = "owner-sid"
    _identity = ("owner-sid", "synthetic-luid")

    def __init__(self, fail=False):
        self.fail = fail

    def verify(self):
        if self.fail:
            raise PermissionError("Owner session locked")


class FakePolicy:
    def __init__(self):
        self.lock = RLock()

    def classify(self, request, owner):
        return Decision.REVIEW


class FakeApproval:
    def __init__(self, request, now=0.0):
        self._request = request
        self.now = now

    def clock(self):
        return self.now

    def consume_local(self, pending_id, context):
        return SimpleNamespace(request=self._request, expires_at=1e9,
                               digest=binding(self._request, context))


class FakeHost:
    def __init__(self, request, start_behavior):
        self.policy = FakePolicy()
        self.approval = FakeApproval(request)
        # The controller pins and revalidates the routed manager under its
        # real lock; a Mock is neither a lock nor an open manager.
        self.jobs = SimpleNamespace(start=start_behavior, _lock=RLock(), _closed=False)
        self._dev_interactive_jobs = None
        self.owner = "owner-sid"

    def _context(self):
        return {}

    def _manager_for_request(self, request):
        return self.jobs


class ControllerMappingTests(unittest.TestCase):
    def setUp(self):
        self.cmd = Command(sys.executable, ("-c", "pass"), os.getcwd(),
                           timeout=5, output_limit=4096, request_id="req-x")
        monitor = patch(
            "sora_commander_next.cli_monitor_bootstrap.ensure_monitor_for_approved_job",
            return_value=None)
        monitor.start()
        self.addCleanup(monitor.stop)

    def make_controller(self, start_behavior, guard_fail=False):
        ctrl = object.__new__(WindowsOwnerController)
        ctrl.host = FakeHost(self.cmd, start_behavior)
        ctrl._guard = FakeGuard(fail=guard_fail)
        return ctrl

    def test_launch_unknown_maps_to_outcome_unknown(self):
        def boom(request):
            raise LaunchOutcomeUnknown("req-x", "resumed", "boom")

        result = self.make_controller(boom).approve_local("pending-1")
        self.assertEqual(result, {"request_id": "req-x",
                                  "status": "OUTCOME_UNKNOWN",
                                  "error": "LaunchOutcomeUnknown",
                                  "launch_phase": "resumed",
                                  "stage": "LAUNCH", "reason": "LAUNCH_UNKNOWN",
                                  "approval_state": "CONSUMED", "launch_outcome": "UNKNOWN"})

    def test_launch_not_started_maps_to_launch_error(self):
        def boom(request):
            raise LaunchNotStarted("req-x", "spawned_suspended", "boom")

        result = self.make_controller(boom).approve_local("pending-1")
        self.assertEqual(result, {"request_id": "req-x",
                                  "status": "ERROR",
                                  "error": "LaunchNotStarted",
                                  "launch_phase": "spawned_suspended",
                                  "stage": "LAUNCH", "reason": "LAUNCH_NOT_STARTED",
                                  "approval_state": "CONSUMED", "launch_outcome": "NOT_STARTED"})

    def test_normal_launch_keeps_review_decision(self):
        def ok(request):
            return {"request_id": "req-x", "status": "RUNNING",
                    "session_id": "a" * 48}

        result = self.make_controller(ok).approve_local("pending-1")
        self.assertEqual(result, {"request_id": "req-x", "status": "RUNNING",
                                  "session_id": "a" * 48,
                                  "decision": "REVIEW",
                                  "stage": "LAUNCH", "reason": "LAUNCH_ACCEPTED",
                                  "approval_state": "CONSUMED", "launch_outcome": "STARTED"})

    def test_owner_lock_stays_auth_error_without_launch(self):
        started = []
        result = self.make_controller(started.append,
                                      guard_fail=True).approve_local("pending-1")
        self.assertEqual(result, {"status": "ERROR",
                                  "error": "OwnerAuthorizationRequired",
                                  "stage": "OWNER_CHECK", "reason": "AUTHORIZATION_FAILED",
                                  "approval_state": "NOT_CONSUMED", "launch_outcome": "NOT_STARTED"})
        self.assertEqual(started, [])

    def test_closed_manager_after_consume_never_launches(self):
        started = []
        ctrl = self.make_controller(started.append)
        ctrl.host.jobs._closed = True
        result = ctrl.approve_local("pending-1")
        self.assertEqual(result["error"], "OwnerAuthorizationRequired")
        self.assertEqual(result["approval_state"], "CONSUMED")
        self.assertEqual(result["launch_outcome"], "NOT_STARTED")
        self.assertEqual(started, [])

    def test_expired_approval_stays_auth_error_without_launch(self):
        started = []
        ctrl = self.make_controller(started.append)
        ctrl.host.approval.now = 2e9
        result = ctrl.approve_local("pending-1")
        self.assertEqual(result, {"status": "ERROR",
                                  "error": "OwnerAuthorizationRequired",
                                  "stage": "CONSUME", "reason": "AUTHORIZATION_FAILED",
                                  "approval_state": "CONSUMED", "launch_outcome": "NOT_STARTED"})
        self.assertEqual(started, [])


if __name__ == "__main__":
    unittest.main()
