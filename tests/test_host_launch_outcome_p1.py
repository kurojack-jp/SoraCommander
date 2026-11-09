"""Bounded Host launch-response checks; every job operation is mocked."""

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.host import Host
from sora_commander_next.jobs import LaunchNotStarted, LaunchOutcomeUnknown
from sora_commander_next.policy import Decision, Pattern, Policy


class HostLaunchOutcomeP1Tests(unittest.TestCase):
    request_id = "0123456789abcdef0123456789abcdef"

    def make_host(self, decision):
        root = str(Path(__file__).resolve().parents[1])
        payload = {"executable": sys.executable, "argv": ("--version",),
                   "cwd": root, "request_id": self.request_id}
        policy = Policy(roots=(root,), auto=(Pattern(sys.executable, ("--version",)),))
        if decision == Decision.SCOPED:
            # No real grant or Owner state is created for this host-only test.
            policy.classify = Mock(return_value=Decision.SCOPED)
        # Host now consults the read-only cross-ID UNKNOWN guard before dispatch.
        # A clean fake must explicitly report no prior ambiguous launch.
        jobs = Mock(spec=["start", "wait", "wait_for", "blocked_unknown_command_readonly"])
        jobs.blocked_unknown_command_readonly.return_value = False
        return Host(policy=policy, jobs=jobs), jobs, payload

    def invoke(self, host, payload, mode):
        if mode == "wait":
            return host.run_command_wait(wait_seconds=0, **payload)
        return host.run_command(background=(mode == "background"), **payload)

    def test_launch_failures_propagate_without_retry_or_message(self):
        for decision in (Decision.AUTO, Decision.SCOPED):
            for mode in ("sync", "background", "wait"):
                for error_type, status, phase in (
                    (LaunchOutcomeUnknown, "OUTCOME_UNKNOWN", "resuming"),
                    (LaunchNotStarted, "ERROR", "pre_spawn"),
                ):
                    with self.subTest(decision=decision, mode=mode, error=error_type):
                        host, jobs, payload = self.make_host(decision)
                        jobs.start.side_effect = error_type(
                            self.request_id, phase, "fake-private-message")
                        result = self.invoke(host, payload, mode)
                        self.assertEqual(result, {
                            "request_id": self.request_id, "status": status,
                            "error": error_type.__name__, "launch_phase": phase})
                        jobs.start.assert_called_once()
                        request = jobs.start.call_args.args[0]
                        self.assertEqual(request.request_id, self.request_id)
                        self.assertEqual(host.policy.classify(request, host.owner), decision)
                        jobs.wait.assert_not_called()
                        jobs.wait_for.assert_not_called()

    def test_generic_error_fallback(self):
        for decision in (Decision.AUTO, Decision.SCOPED):
            for mode in ("sync", "background", "wait"):
                with self.subTest(decision=decision, mode=mode):
                    host, jobs, payload = self.make_host(decision)
                    jobs.start.side_effect = RuntimeError("fake-private-message")
                    self.assertEqual(self.invoke(host, payload, mode), {
                        "request_id": self.request_id, "status": "ERROR",
                        "error": "RuntimeError"})
                    jobs.start.assert_called_once()
                    jobs.wait.assert_not_called()
                    jobs.wait_for.assert_not_called()

    def test_successful_session_wait_routing(self):
        for decision in (Decision.AUTO, Decision.SCOPED):
            for mode in ("sync", "background", "wait"):
                with self.subTest(decision=decision, mode=mode):
                    host, jobs, payload = self.make_host(decision)
                    running = {"request_id": self.request_id, "status": "RUNNING",
                               "session_id": "a" * 48}
                    completed = {**running, "status": "COMPLETED"}
                    jobs.start.return_value = running.copy()
                    jobs.wait.return_value = completed.copy()
                    jobs.wait_for.return_value = completed.copy()
                    result = self.invoke(host, payload, mode)
                    self.assertEqual(result, {
                        **(running if mode == "background" else completed),
                        "decision": decision.value})
                    jobs.start.assert_called_once()
                    if mode == "sync":
                        jobs.wait.assert_called_once_with(running["session_id"])
                        jobs.wait_for.assert_not_called()
                    elif mode == "wait":
                        jobs.wait.assert_not_called()
                        jobs.wait_for.assert_called_once_with(running["session_id"], 0.0)
                    else:
                        jobs.wait.assert_not_called()
                        jobs.wait_for.assert_not_called()


if __name__ == "__main__":
    unittest.main()
