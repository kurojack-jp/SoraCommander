"""No GUI clicks: explicit local dispatcher attachment and fail closed."""
import pathlib,sys,tempfile,unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.owner_review_dispatch import OwnerReviewDispatcher
from sora_commander_next.windows_owner_controller import WindowsOwnerController
from sora_commander_next.windows_owner import OwnerSessionGuard

class HostDispatchTests(unittest.TestCase):
 def test_attached_dispatch_receives_review_without_auto_approval(self):
  with tempfile.TemporaryDirectory() as root:
   guard=OwnerSessionGuard();guard.verify()
   host=Host(policy=Policy(roots=(root,)),owner=guard.sid)
   try:
    dispatcher=OwnerReviewDispatcher(WindowsOwnerController(host))
    host.attach_owner_review_local(dispatcher)
    with patch.object(dispatcher,"offer_local",return_value=True) as offer:
     result=host.run_command(executable=sys.executable,argv=["-c","print(345)"],cwd=root)
     self.assertEqual(result["status"],"REVIEW")
     offer.assert_called_once_with(result["pending_id"])
   finally:host.jobs.close()
 def test_reject_if_local_dispatch_unavailable(self):
  with tempfile.TemporaryDirectory() as root:
   guard=OwnerSessionGuard();guard.verify()
   host=Host(policy=Policy(roots=(root,)),owner=guard.sid)
   try:
    dispatcher=OwnerReviewDispatcher(WindowsOwnerController(host));host.attach_owner_review_local(dispatcher)
    with patch.object(dispatcher,"offer_local",return_value=False):
     result=host.run_command(executable=sys.executable,argv=["-c","print(123)"],cwd=root)
     self.assertEqual(result["status"],"ERROR")
     self.assertEqual(len(host.approval.pending_local()),0)
   finally:host.jobs.close()
if __name__=="__main__":unittest.main()
