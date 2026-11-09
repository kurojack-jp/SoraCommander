"""Phase 7 boundary tests independent of MCP SDK."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.host_supervisor import HostSupervisor


class Phase7BoundaryTests(unittest.TestCase):
    def test_bad_local_startup_token_is_rejected_before_process_launch(self):
        bad = (False, True, None, "a" * 64, b"x" * 64, b"0" * 64,
               b"0123456789abcdef" * 4, b"A" * 64, b"x" * 63 + b"\n",
               b"c" * 63, b"c" * 64 + b"\n")
        for token in bad:
            if token is None:
                continue  # None explicitly selects the original no-token mode
            with self.subTest(token=type(token).__name__), self.assertRaises(ValueError):
                HostSupervisor([sys.executable], str(ROOT), startup_token=token)

    def test_valid_token_does_not_leak_into_snapshot(self):
        token = b"0123456789abcdef" + b"fedcba9876543210" + b"13579bdf02468ace" + b"eca86420fdb97531"
        watcher = HostSupervisor([sys.executable], str(ROOT), startup_token=token)
        self.assertNotIn(token.decode(), str(watcher.snapshot()))
        self.assertEqual(watcher.snapshot()["state"], "NOT_STARTED")
        self.assertFalse(watcher.snapshot()["remote_ready"])
        self.assertFalse(watcher.snapshot()["replay"])


if __name__ == "__main__":
    unittest.main()
