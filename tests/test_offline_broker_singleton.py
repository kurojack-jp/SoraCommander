"""Windows-only DEV mutex proof: no privileged Broker, IPC, or commands."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

from sora_commander_next.offline_broker_singleton import DevLeaseError, DevWindowsMutexLease

_CHILD = (
    "import sys;"
    "sys.path.insert(0,sys.argv[2]);"
    "from sora_commander_next.offline_broker_singleton import DevWindowsMutexLease;"
    "l=DevWindowsMutexLease(sys.argv[1]);"
    "ok=l.try_acquire();"
    "print('ACQUIRED' if ok else 'DENIED');"
    "l.close()"
)


@unittest.skipUnless(os.name == "nt", "Windows mutex test")
class DevSingletonTests(unittest.TestCase):
    def setUp(self):
        self.name = "Local\\SoraCommanderNext.DevLease." + uuid.uuid4().hex
        self.src = str(Path(__file__).resolve().parents[1] / "src")

    def child_result(self):
        p = subprocess.run(
            [sys.executable, "-c", _CHILD, self.name, self.src],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def test_distinct_process_denied_while_held_then_allowed(self):
        with DevWindowsMutexLease(self.name) as lease:
            self.assertEqual(self.child_result(), "DENIED")
            with self.assertRaises(DevLeaseError):
                lease.try_acquire()
        self.assertEqual(self.child_result(), "ACQUIRED")

    def test_new_name_is_not_shared_and_close_is_idempotent(self):
        first = DevWindowsMutexLease(self.name)
        self.assertTrue(first.try_acquire())
        other = DevWindowsMutexLease(self.name + "alt")
        self.assertTrue(other.try_acquire())
        other.close()
        first.close()
        first.close()
        self.assertEqual(self.child_result(), "ACQUIRED")

    def test_only_disposable_local_namespace_names(self):
        for invalid in ("Global\\SoraCommanderNext.DevLease.test",
                        "Local\\SoraCommanderNext.Production",
                        "Local\\SoraCommanderNext.DevLease.",
                        "Local\\SoraCommanderNext.DevLease." + "x" * 65,
                        "", None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    DevWindowsMutexLease(invalid)


if __name__ == "__main__":
    unittest.main()
