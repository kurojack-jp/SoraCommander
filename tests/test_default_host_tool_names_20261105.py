"""The default Host's public tools, by exact name. Reads registered metadata: nothing runs and no window opens.

Adding, removing or replacing a tool must change this list on purpose, in review.
tests/test_mcp_ai_tool_catalog.py covers the 44-tool personal DEV configuration.
"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import build_server

DEFAULT_TOOLS = frozenset({
    # Read-only additions; neither registration nor reads open Owner windows.
    "git_snapshot", "git_diff", "git_log", "git_show", "git_remote_check", "list_tasks", "read_image_file",
    # Registered by default, but execution still requires Owner approval.
    "run_task", "ogf_cue",
    "await_command_result", "await_process_output", "compare_text_files", "create_directory", "create_file",
    "find_files", "get_config", "list_folder", "list_folder_page", "list_processes", "list_sessions", "move_file",
    "path_info", "ping", "preview_source_create", "preview_source_patch", "preview_text_edit", "previous_output",
    "previous_sessions", "read_many_text_files", "read_process_output", "read_text_chunk", "read_text_file",
    "read_text_lines", "read_text_page", "recovery_overview", "request_source_patch_dev", "request_status",
    "review_status", "run_command", "run_command_wait", "search_code", "send_input", "sha256_file", "status",
    "stop_process", "stop_process_wait", "tool_catalog",
})
NEVER_REMOTE = frozenset({"approve_local", "reject_local", "add_grant_local", "replace_text"})


class DefaultHostToolNames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.host = Host()
        cls.addClassCleanup(cls.host.jobs.close)
        cls.names = [tool.name for tool in build_server(cls.host)._tool_manager.list_tools()]

    def test_the_exact_names(self):
        self.assertEqual(set(self.names), set(DEFAULT_TOOLS))

    def test_no_name_is_registered_twice(self):
        self.assertEqual(len(self.names), len(set(self.names)))

    def test_local_only_actions_are_never_public(self):
        self.assertFalse(NEVER_REMOTE & set(self.names))
        self.assertFalse(NEVER_REMOTE & DEFAULT_TOOLS)


if __name__ == "__main__":
    unittest.main()
