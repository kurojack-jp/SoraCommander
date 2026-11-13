"""D10 live-facing, data-only SDK registry diagnostic (not remote attestation)."""
import asyncio
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
import re
import unittest
from unittest.mock import patch

from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import (
    _local_registered_catalog, _validate_arguments, build_server,
)
from sora_commander_next.edit_review_pilot import EditReviewPilot


class LiveCatalogTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(inherited_test_temp())).resolve()

    def test_actual_runtime_registry_catalog_matches_server_and_status(self):
        host=Host(policy=Policy(roots=(str(self.root),)))
        try:
            server=build_server(host)
            names=sorted(tool.name for tool in server._tool_manager.list_tools())
            self.assertEqual(len(names),48)
            _,data=asyncio.run(server.call_tool("tool_catalog",{}))
            _,status=asyncio.run(server.call_tool("status",{}))
            self.assertEqual(data["registered_tool_count"],len(names))
            self.assertEqual(data["registered_tool_names"],names)
            self.assertEqual(data["status"],"LOCAL_REGISTRATION_SNAPSHOT")
            self.assertEqual(data["chatgpt_tool_visibility"],"UNVERIFIED")
            self.assertEqual(data["protected_broker_acceptance"],
                             "NOT_ESTABLISHED_BY_CATALOG")
            self.assertTrue(data["read_roots_configured"])
            self.assertFalse(data["edit_pilot_attached"])
            self.assertRegex(data["tool_schema_sha256"],r"^[0-9a-f]{64}$")
            self.assertEqual(status["registered_tool_count"],len(names))
            self.assertEqual(status["tool_schema_sha256"],data["tool_schema_sha256"])
            self.assertEqual(status["tool_catalog_scope"],data["status"])
            self.assertFalse(status["remote_ready"])
            self.assertEqual(status["approval"],"LOCAL_DEMO_ONLY")
            self.assertEqual(status["chatgpt_tool_visibility"],"UNVERIFIED")
            self.assertNotIn(str(self.root),repr(data))
        finally:
            host.jobs.close()

    def test_exact_repeat_uses_same_registration_fingerprint(self):
        host=Host()
        try:
            a=build_server(host)
            b=build_server(host)
            one=_local_registered_catalog(a,host)
            two=_local_registered_catalog(b,host)
            self.assertEqual(one["tool_schema_sha256"],two["tool_schema_sha256"])
            self.assertEqual(one["registered_tool_names"],two["registered_tool_names"])
            self.assertFalse(one["read_roots_configured"])
            self.assertIs(type(one["installed_distribution_version"]),str)
            self.assertGreater(len(one["installed_distribution_version"]),0)
        finally:
            host.jobs.close()

    def test_pilot_registration_changes_actual_count_and_digest(self):
        base=Host()
        owner=Host(policy=Policy(roots=(str(self.root),)))
        owner._edit_review_pilot=EditReviewPilot(str(self.root),enable_create=True)
        try:
            a=_local_registered_catalog(build_server(base),base)
            b=_local_registered_catalog(build_server(owner),owner)
            self.assertGreater(b["registered_tool_count"],a["registered_tool_count"])
            self.assertNotEqual(b["tool_schema_sha256"],a["tool_schema_sha256"])
            self.assertTrue(b["edit_pilot_attached"])
            for name in ("request_text_edit","request_text_create",
                         "await_edit_result"):
                self.assertIn(name,b["registered_tool_names"])
                self.assertNotIn(name,a["registered_tool_names"])
        finally:
            base.jobs.close()
            owner.jobs.close()

    def test_registration_changes_after_server_creation_not_static_source(self):
        host=Host()
        try:
            sdk=build_server(host)
            before=_local_registered_catalog(sdk,host)
            @sdk.tool()
            def local_diagnostic_fixture(value: str) -> dict:
                return {"value":value}
            after=_local_registered_catalog(sdk,host)
            self.assertEqual(after["registered_tool_count"],
                             before["registered_tool_count"]+1)
            self.assertNotEqual(after["tool_schema_sha256"],
                                before["tool_schema_sha256"])
            self.assertIn("local_diagnostic_fixture",after["registered_tool_names"])
        finally:
            host.jobs.close()

    def test_tool_catalog_has_no_wire_arguments_or_launch(self):
        from mcp.server.fastmcp.exceptions import ToolError
        host=Host()
        try:
            sdk=build_server(host)
            for invalid in ({"path":"C:/private"}, {"approve":True},
                            {"expected_sha256":"x"}):
                with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                    _validate_arguments("tool_catalog",invalid)
                with self.assertRaises(ToolError):
                    asyncio.run(sdk.call_tool("tool_catalog",invalid))
            with patch("subprocess.Popen") as spawn:
                _,cat=asyncio.run(sdk.call_tool("tool_catalog",{}))
                _,status=asyncio.run(sdk.call_tool("status",{}))
                self.assertGreater(cat["registered_tool_count"],0)
                self.assertEqual(status["registered_tool_count"],
                                 cat["registered_tool_count"])
                spawn.assert_not_called()
        finally:
            host.jobs.close()


if __name__=="__main__":
    unittest.main()
