"""Phase10M: local dispatcher never approves in tests; no GUI click or remote link."""
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.windows_owner import OwnerSessionGuard
from sora_commander_next.windows_owner_controller import WindowsOwnerController
from sora_commander_next.owner_review_dispatch import OwnerReviewDispatcher


class OwnerDispatchTests(unittest.TestCase):
    def test_invalid_pending_cannot_open_dialog(self):
        with tempfile.TemporaryDirectory() as root:
            guard = OwnerSessionGuard()
            guard.verify()
            host = Host(policy=Policy(roots=(root,)), owner=guard.sid)
            try:
                controller = WindowsOwnerController(host)
                dispatcher = OwnerReviewDispatcher(controller)
                with patch.object(dispatcher._broker, "review_once") as review:
                    self.assertFalse(dispatcher.offer_local("nonexistent"))
                    self.assertFalse(dispatcher.offer_local(None))
                    self.assertIsNone(dispatcher.result_local())
                    review.assert_not_called()
            finally:
                host.jobs.close()

    def test_review_is_not_automatically_approved(self):
        with tempfile.TemporaryDirectory() as root:
            guard = OwnerSessionGuard()
            guard.verify()
            host = Host(policy=Policy(roots=(root,)), owner=guard.sid)
            try:
                controller = WindowsOwnerController(host)
                dispatcher = OwnerReviewDispatcher(controller)
                result = host.run_command(executable=sys.executable, argv=["-c", "print(42)"], cwd=root)
                self.assertEqual(result["status"], "REVIEW")
                with patch.object(dispatcher._broker, "review_once", return_value={"status": "DENIED"}) as review:
                    self.assertTrue(dispatcher.offer_local(result["pending_id"]))
                    dispatcher._active.join(timeout=3)
                    self.assertFalse(dispatcher._active.is_alive())
                    review.assert_called_once_with(result["pending_id"])
                    self.assertEqual(dispatcher.result_local()["status"], "DENIED")
            finally:
                host.jobs.close()


if __name__ == "__main__":
    unittest.main()
