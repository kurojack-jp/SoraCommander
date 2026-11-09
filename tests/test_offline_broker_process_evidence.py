"""DEV-only Windows process evidence tests; no live Broker or IPC."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

from sora_commander_next.offline_broker_process_evidence import (
    DevProcessEvidenceRejected, observe_owner_process)
from sora_commander_next.windows_owner import OwnerSessionGuard


@unittest.skipUnless(os.name == "nt", "Win32 process identity observations only")
class ProcessEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.guard = OwnerSessionGuard()
        self.guard.verify()
        self.child = None
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.child is not None:
            if self.child.poll() is None:
                self.child.kill()
            self.child.wait(timeout=5)
            for stream in (self.child.stdout, self.child.stderr):
                if stream and not stream.closed:
                    stream.close()

    def start_probe(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = (str(root / "src") + os.pathsep
                                  + env.get("PYTHONPATH", ""))
        name = "Local\\SoraCommanderNext.DevLease." + uuid.uuid4().hex
        self.child = subprocess.Popen(
            [sys.executable, "-m", "sora_commander_next.offline_broker_owner_probe",
             "--name", name, "--hold-seconds", "4"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.assertEqual(self.child.stdout.readline().strip(),
                         "DEV_OWNER_VERIFIED")
        return self.child

    def test_current_windows_pid_image_and_create_time_observed(self):
        # Windows virtualenv sys.executable may be the launcher path while
        # QueryFullProcessImageNameW reports sys._base_executable. A caller
        # must pin the *actual* image, not infer it from a Python venv shim.
        image_path = getattr(sys, "_base_executable", sys.executable)
        evidence = observe_owner_process(
            os.getpid(), expected_executable=image_path,
            owner_guard=self.guard)
        if os.path.normcase(sys.executable) != os.path.normcase(image_path):
            with self.assertRaises(DevProcessEvidenceRejected):
                observe_owner_process(
                    os.getpid(), expected_executable=sys.executable,
                    owner_guard=self.guard)
        self.assertEqual(evidence.pid, os.getpid())
        self.assertTrue(evidence.still_running)
        self.assertTrue(evidence.owner_token_matches)
        self.assertGreater(evidence.created_filetime, 0)
        self.assertFalse(evidence.trusted_publisher_verified)
        self.assertFalse(evidence.protected_install_verified)
        self.assertFalse(evidence.broker_authorized)

    def test_running_independent_dev_owner_child_has_stable_identity(self):
        child = self.start_probe()
        first = observe_owner_process(
            child.pid, expected_executable=sys.executable,
            owner_guard=self.guard)
        second = observe_owner_process(
            child.pid, expected_executable=sys.executable,
            owner_guard=self.guard)
        self.assertEqual(first.created_filetime, second.created_filetime)
        self.assertEqual(first.image_path, second.image_path)
        self.assertEqual(first.pid, child.pid)
        self.assertIsNone(child.poll())

    def test_wrong_executable_and_bad_pid_fail_closed(self):
        child = self.start_probe()
        for name in ("C:/Windows/System32/not-this-child.exe",
                     "relative.exe"):
            with self.subTest(name=name), self.assertRaises(
                    DevProcessEvidenceRejected):
                observe_owner_process(
                    child.pid, expected_executable=name, owner_guard=self.guard)
        for pid in (0, -1, True, "5"):
            with self.subTest(pid=pid), self.assertRaises(
                    DevProcessEvidenceRejected):
                observe_owner_process(
                    pid, expected_executable=sys.executable,
                    owner_guard=self.guard)

    def test_exited_child_not_accepted_or_reused(self):
        child = self.start_probe()
        child.kill()
        child.wait(timeout=5)
        with self.assertRaises(DevProcessEvidenceRejected):
            observe_owner_process(
                child.pid, expected_executable=sys.executable,
                owner_guard=self.guard)

    def test_missing_owner_state_denies_before_process_image(self):
        child = self.start_probe()
        with patch.object(self.guard, "verify",
                          side_effect=PermissionError("simulated lock")):
            with self.assertRaises(PermissionError):
                observe_owner_process(
                    child.pid, expected_executable=sys.executable,
                    owner_guard=self.guard)


if __name__ == "__main__":
    unittest.main()
