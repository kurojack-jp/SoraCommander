"""Actual Windows Owner-token process probe; DEV ONLY, never production IPC."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

from sora_commander_next.windows_owner import OwnerSessionGuard
from sora_commander_next import offline_broker_owner_probe as probe


@unittest.skipUnless(os.name == "nt", "Windows Owner identity probe")
class DevOwnerProcessTests(unittest.TestCase):
    def setUp(self):
        self.name = "Local\\SoraCommanderNext.DevLease." + uuid.uuid4().hex
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = (str(Path(__file__).resolve().parents[1] / "src")
                                  + os.pathsep + self.env.get("PYTHONPATH", ""))
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

    def cmd(self, seconds="1.5", name=None):
        return [sys.executable, "-m", "sora_commander_next.offline_broker_owner_probe",
                "--name", name or self.name, "--hold-seconds", seconds]

    def test_os_owner_validated_in_parent_and_live_child(self):
        guard = OwnerSessionGuard()
        child = subprocess.Popen(self.cmd(), env=self.env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(), "DEV_OWNER_VERIFIED")
        self.assertTrue(guard.verify_process(child.pid))
        competing = subprocess.run(self.cmd("0.5"), env=self.env, timeout=5,
                                   capture_output=True, text=True, check=False)
        self.assertEqual(competing.returncode, 3, competing.stderr)
        self.assertIn("DEV_LEASE_HELD_OR_UNKNOWN", competing.stdout)
        child.stdout.close()  # Observer disconnect, not an Owner session change.
        self.assertIsNone(child.poll())
        self.assertEqual(child.wait(timeout=5), 0)
        guard.verify()
        after = subprocess.run(self.cmd("0.5"), env=self.env, timeout=5,
                               capture_output=True, text=True, check=False)
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(after.stdout.strip(), "DEV_OWNER_VERIFIED")

    def test_owner_lock_recheck_denies_injected_unavailable_guard(self):
        class FailAfterReady:
            calls = 0
            def verify(self):
                self.calls += 1
                if self.calls >= 3:
                    raise PermissionError("simulated Owner lock")
        output = io.StringIO()
        with patch.object(probe, "OwnerSessionGuard", return_value=FailAfterReady()):
            with contextlib.redirect_stdout(output):
                code = probe.main(["--name", self.name, "--hold-seconds", "0.5"])
        self.assertEqual(code, 4)
        self.assertEqual(output.getvalue().splitlines(),
                         ["DEV_OWNER_VERIFIED", "DEV_OWNER_UNAVAILABLE"])

    def test_untrusted_name_and_invalid_duration_never_open_owner(self):
        for name, seconds in (
            ("Global\\SoraCommanderNext.Production", "0.5"),
            (self.name, "nan"), (self.name, "inf"), (self.name, "100"),
        ):
            with self.subTest(name=name, seconds=seconds):
                result = subprocess.run(self.cmd(seconds, name=name), env=self.env,
                                        timeout=5, capture_output=True,
                                        text=True, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("DEV_OWNER_VERIFIED", result.stdout)


if __name__ == "__main__":
    unittest.main()
