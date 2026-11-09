"""Real MCP SDK returns bounded preview error data without changing files."""
import asyncio
import sys
from hashlib import sha256
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import build_server


class PreviewErrorMCPTests(TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
    def test_stale_sha_is_structured_and_host_survives(self):
        with inherited_test_temp() as root:
            target = Path(root) / "owner-edit-pilot-preview.txt"
            target.write_bytes(b"current\n")
            host = Host(policy=Policy(roots=(str(Path(root).resolve()),)))
            try:
                server = build_server(host)
                result = asyncio.run(server.call_tool("preview_text_edit", {
                    "path": str(target), "content": "proposed\n",
                    "expected_sha256": sha256(b"old\n").hexdigest()
                }))
                self.assertIn("STALE_SHA256", str(result))
                self.assertIn("ERROR", str(result))
                self.assertNotIn(str(target), str(result))
                self.assertNotIn("proposed", str(result))
                self.assertEqual(target.read_bytes(), b"current\n")
                self.assertIn("OK", str(asyncio.run(server.call_tool("ping", {}))))
            finally:
                host.jobs.close()

    def test_out_of_root_rejected_without_path_leak(self):
        with inherited_test_temp() as root, inherited_test_temp() as elsewhere:
            target = Path(elsewhere) / "secret.txt"
            target.write_bytes(b"not authorized\n")
            host = Host(policy=Policy(roots=(str(Path(root).resolve()),)))
            try:
                server = build_server(host)
                result = asyncio.run(server.call_tool("preview_text_edit", {
                    "path": str(target), "content": "attack",
                    "expected_sha256": sha256(b"not authorized\n").hexdigest()
                }))
                self.assertIn("PREVIEW_REJECTED", str(result))
                self.assertNotIn(str(target), str(result))
                self.assertNotIn("not authorized", str(result))
                self.assertEqual(target.read_bytes(), b"not authorized\n")
            finally:
                host.jobs.close()

    def test_valid_preview_remains_read_only(self):
        with inherited_test_temp() as root:
            target = Path(root) / "owner-edit-pilot-preview.txt"
            target.write_bytes(b"old\n")
            host = Host(policy=Policy(roots=(str(Path(root).resolve()),)))
            try:
                server = build_server(host)
                result = asyncio.run(server.call_tool("preview_text_edit", {
                    "path": str(target), "content": "new\n",
                    "expected_sha256": sha256(b"old\n").hexdigest()
                }))
                self.assertIn("new_sha256", str(result))
                self.assertIn("+new", str(result))
                self.assertEqual(target.read_bytes(), b"old\n")
            finally:
                host.jobs.close()
