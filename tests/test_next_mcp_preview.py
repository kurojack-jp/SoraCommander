"""Read-only proposed edit preview through MCP, never an edit action."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from hashlib import sha256
from tempfile import TemporaryDirectory
from unittest import TestCase
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import build_server, _validate_arguments

class PreviewMCPTests(TestCase):
    def test_preview_tool_registered_and_no_write_tool(self):
        with TemporaryDirectory() as root:
            h=Host(policy=Policy(roots=(str(Path(root).resolve()),)))
            try:
                server=build_server(h)
                tools={tool.name for tool in server._tool_manager.list_tools()}
                self.assertIn('preview_text_edit',tools)
                self.assertNotIn('replace_text',tools)
                self.assertNotIn('approve_file_edit',tools)
            finally:
                h.jobs.close()
    def test_preview_wire_requires_exact_arguments(self):
        good={'path':'C:/test.txt','content':'changed','expected_sha256':sha256(b'original').hexdigest()}
        _validate_arguments('preview_text_edit',good)
        for bad in ({**good,'approve':True},{**good,'content':None}):
            with self.assertRaises(ValueError):
                _validate_arguments('preview_text_edit',bad)
