"""No-op source patches must not enter an Owner review or reach a writer."""
from hashlib import sha256
import unittest
import test_sol_broker_vertical as fixtures
from sora_commander_next.source_review import SourceReviewWorkflow, SourcePolicy, SourceConflict

class NoOpSourceReviewTests(unittest.TestCase):
    def test_identical_literal_is_denied_before_review(self):
        store=fixtures.AtomicStore()
        store.seed(fixtures.TARGET,b"value = 1\n")
        owner=fixtures.Owner()
        flow=SourceReviewWorkflow(policy=SourcePolicy((fixtures.ALLOW,)),
                                  store=store,owner=owner,clock=lambda:0.1)
        original=b"value = 1\n"
        args={"path":fixtures.TARGET,"old_text":"1","new_text":"1",
              "expected_sha256":sha256(original).hexdigest(),
              "expected_new_sha256":sha256(original).hexdigest(),
              "request_id":"f"*32}
        with self.assertRaises(SourceConflict):
            flow.submit("request_source_patch",args)
        self.assertEqual(store.writes,0)
        self.assertEqual(owner.reviews,[])
        self.assertEqual(flow._records,{})
