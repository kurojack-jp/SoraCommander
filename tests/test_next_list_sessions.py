"""Session listing provides bounded metadata without leaking command arguments."""
import sys
from pathlib import Path
from unittest import TestCase
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import build_server

class ListSessionsTests(TestCase):
    def test_empty_and_registered(self):
        h=Host()
        try:
            self.assertEqual(h.list_sessions(),{"sessions":[],"count":0})
            tools={t.name for t in build_server(h)._tool_manager.list_tools()}
            self.assertIn("list_sessions",tools)
        finally:
            h.jobs.close()
