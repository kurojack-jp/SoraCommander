"""MCP review result reporting never supplies Owner authorization."""
import unittest
from unittest.mock import Mock
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import _validate_arguments, build_server


class ReviewStatusTests(unittest.TestCase):
    def test_default_server_does_not_publish_authorization(self):
        host = Host()
        try:
            self.assertEqual(host.review_status("a" * 32), {"status": "DENY"})
            server = build_server(host)
            tools = {tool.name for tool in server._tool_manager.list_tools()}
            self.assertIn("review_status", tools)
            self.assertNotIn("approve", tools)
            self.assertNotIn("reject", tools)
        finally:
            host.jobs.close()

    def test_review_status_wire_argument_validation(self):
        _validate_arguments("review_status", {"pending_id": "a" * 32})
        for invalid in ({}, {"pending_id": 5},
                        {"pending_id": "a" * 32, "approve": True}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _validate_arguments("review_status", invalid)


if __name__ == "__main__":
    unittest.main()
