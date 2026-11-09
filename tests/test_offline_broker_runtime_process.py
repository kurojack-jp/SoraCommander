"""DEV-only child process owning actual BrokerRuntime and JobManager.

Tests use only a fixed harmless Python fixture in a temporary cwd.
No GUI, protected Owner data, remote listener, or general command intake.
"""
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

from sora_commander_next.windows_owner import OwnerSessionGuard


@unittest.skipUnless(os.name == "nt", "Windows-only Owner Broker integration probe")
class DevIndependentBrokerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.name = "Local\\SoraCommanderNext.DevLease." + uuid.uuid4().hex
        self.env = dict(os.environ)
        src = str(Path(__file__).resolve().parents[1] / "src")
        self.env["PYTHONPATH"] = src + os.pathsep + self.env.get("PYTHONPATH", "")
        self.children = []
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=6)
            for stream in (child.stdout, child.stderr):
                if stream and not stream.closed:
                    stream.close()

    def cmd(self, name=None):
        return [sys.executable, "-m",
                "sora_commander_next.offline_broker_runtime_process",
                "--name", self.name if name is None else name]

    def test_real_broker_job_result_in_independent_owner_process(self):
        result = subprocess.run(self.cmd(), env=self.env, capture_output=True,
                                text=True, timeout=12, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         ["DEV_BROKER_JOB_STARTED", "DEV_BROKER_RESULT_VERIFIED"])

    def test_live_process_verified_and_duplicate_denied(self):
        owner_guard = OwnerSessionGuard()
        child = subprocess.Popen(self.cmd(), env=self.env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(), "DEV_BROKER_JOB_STARTED")
        self.assertTrue(owner_guard.verify_process(child.pid))
        self.assertIsNone(child.poll())
        competing = subprocess.run(self.cmd(), env=self.env, capture_output=True,
                                   text=True, timeout=6, check=False)
        self.assertEqual(competing.returncode, 3, competing.stderr)
        self.assertEqual(competing.stdout.strip(), "DEV_BROKER_LEASE_DENIED")
        self.assertEqual(child.wait(timeout=7), 0)
        self.assertEqual(child.stdout.read().strip(), "DEV_BROKER_RESULT_VERIFIED")

    def test_observer_disconnect_does_not_kill_broker_owned_job(self):
        child = subprocess.Popen(self.cmd(), env=self.env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(), "DEV_BROKER_JOB_STARTED")
        child.stdout.close()
        # Broker remains responsible for its real JobManager and fixed job.
        self.assertIsNone(child.poll())
        code = child.wait(timeout=8)
        self.assertEqual(code, 0, child.stderr.read())

    def test_non_dev_mutex_namespace_refused_before_job(self):
        result = subprocess.run(self.cmd("Global\\SoraCommanderNext.Prod"),
                                env=self.env, capture_output=True,
                                text=True, timeout=6, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("DEV_BROKER_JOB_STARTED", result.stdout)


if __name__ == "__main__":
    unittest.main()
