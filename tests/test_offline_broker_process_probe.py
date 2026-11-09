"""DEV-only child-process lifetime proof, no real Broker, commands or IPC."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid


@unittest.skipUnless(os.name == "nt", "Windows-only DEV process probe")
class DevBrokerProcessProbeTests(unittest.TestCase):
    def setUp(self):
        self.name = "Local\\SoraCommanderNext.DevLease." + uuid.uuid4().hex
        self.env = dict(os.environ)
        root = str(Path(__file__).resolve().parents[1] / "src")
        self.env["PYTHONPATH"] = root + os.pathsep + self.env.get("PYTHONPATH", "")
        self.children = []
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            if child.stdout and not child.stdout.closed:
                child.stdout.close()
            if child.stderr and not child.stderr.closed:
                child.stderr.close()

    def cmd(self, *, name=None, hold="2.5"):
        return [sys.executable, "-m",
                "sora_commander_next.offline_broker_process_probe",
                "--name", name or self.name, "--hold-seconds", hold]

    def test_process_survives_observer_pipe_disconnect_and_denies_second(self):
        owner = subprocess.Popen(
            self.cmd(), env=self.env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.children.append(owner)
        self.assertEqual(owner.stdout.readline().strip(), "PROBE_READY")
        # A different OS process cannot acquire the active mutex.
        duplicate = subprocess.run(
            self.cmd(hold="0.5"), env=self.env, timeout=5,
            capture_output=True, text=True, check=False)
        self.assertEqual(duplicate.returncode, 3, duplicate.stderr)
        self.assertEqual(duplicate.stdout.strip(), "PROBE_HELD_OR_UNKNOWN")
        owner.stdout.close()  # Simulated observer disconnect; child owns its lease.
        self.assertIsNone(owner.poll(), "observer closing its pipe must not kill child")
        self.assertEqual(owner.wait(timeout=6), 0)
        # After independently completing, same name becomes available again.
        after = subprocess.run(
            self.cmd(hold="0.5"), env=self.env, timeout=5,
            capture_output=True, text=True, check=False)
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(after.stdout.strip(), "PROBE_READY")

    def test_nonfinite_and_long_lifetime_refused(self):
        for duration in ("nan", "inf", "-inf", "0", "30"):
            with self.subTest(duration=duration):
                result = subprocess.run(
                    self.cmd(hold=duration), env=self.env, timeout=5,
                    capture_output=True, text=True, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("PROBE_READY", result.stdout)

    def test_production_namespace_rejected(self):
        result = subprocess.run(
            self.cmd(name="Global\\SoraCommanderNext.BrokerProd", hold="0.5"),
            env=self.env, timeout=5, capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PROBE_READY", result.stdout)


if __name__ == "__main__":
    unittest.main()
