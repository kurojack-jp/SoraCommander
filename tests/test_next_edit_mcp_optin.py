"""MCP edit tools are absent by default and constrained to the opt-in pilot."""
import asyncio
from hashlib import sha256
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from unittest import TestCase
from unittest.mock import patch
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import build_server
from sora_commander_next.edit_review_pilot import EditReviewPilot

class EditMCPOptInTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
    def test_default_has_no_write_tool(self):
        host=Host()
        try:
            names={t.name for t in build_server(host)._tool_manager.list_tools()}
            self.assertNotIn("request_text_edit",names)
            self.assertNotIn("edit_review_status",names)
        finally:
            host.jobs.close()

    def test_enabled_pilot_is_fixture_only_and_fails_closed(self):
        with inherited_test_temp() as folder:
            root=Path(folder)
            p=root/"owner-edit-pilot-fixture.txt"
            p.write_bytes(b"before\n")
            host=Host()
            host._edit_review_pilot=EditReviewPilot(root)
            try:
                server=build_server(host)
                names={t.name for t in server._tool_manager.list_tools()}
                self.assertIn("request_text_edit",names)
                self.assertIn("edit_review_status",names)
                async def call(name,args):
                    _,result=await server.call_tool(name,args)
                    return result
                old=sha256(b"before\n").hexdigest()
                bad=asyncio.run(call("request_text_edit",{"path":str(root/"other.txt"),
                     "content":"after\n","expected_sha256":old}))
                self.assertEqual(bad["error"],"EDIT_REJECTED")
                stale=asyncio.run(call("request_text_edit",{"path":str(p),
                     "content":"after\n","expected_sha256":"0"*64}))
                self.assertEqual(stale["error"],"STALE_SHA256")
                with patch("sora_commander_next.edit_review_pilot.review_and_replace_local",
                           return_value={"status":"DENIED"}):
                    result=asyncio.run(call("request_text_edit",{"path":str(p),
                        "content":"after\n","expected_sha256":old}))
                    self.assertEqual(result["status"],"REVIEW")
                    host._edit_review_pilot._results[result["pending_id"]]["status"]="DENIED"
                outcome=asyncio.run(call("edit_review_status",{"pending_id":result["pending_id"]}))
                self.assertEqual(outcome["status"],"DENIED")
                self.assertEqual(p.read_bytes(),b"before\n")
            finally:
                host.jobs.close()
