"""Local PNG branding fixtures; no real Owner artwork attached or modified."""
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.plugin_icon import (
    PluginIconError, inspect_icon_png, pinned_plugin_icon
)


def png_bytes(width=32, height=32, color=(27, 103, 160, 255)):
    def chunk(name, data):
        return (struct.pack(">I",len(data)) + name + data +
                struct.pack(">I",zlib.crc32(name+data)&0xFFFFFFFF))
    header=struct.pack(">IIBBBBB",width,height,8,6,0,0,0)
    raw=b"".join(b"\0"+bytes(color)*width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR",header)
            + chunk(b"IDAT",zlib.compress(raw))
            + chunk(b"IEND",b""))


class PluginBrandingTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix="sc-icon-")
        self.addCleanup(temp.cleanup)
        self.icon=Path(temp.name)/"test.png"
        self.icon.write_bytes(png_bytes())
        self.digest=hashlib.sha256(self.icon.read_bytes()).hexdigest()

    def test_pinned_icon_generates_mcp_metadata_without_network(self):
        metadata=pinned_plugin_icon(self.icon,expected_sha256=self.digest)
        self.assertEqual(metadata["mime_type"],"image/png")
        self.assertEqual(metadata["sizes"],["32x32"])
        self.assertTrue(metadata["src"].startswith("data:image/png;base64,"))

    def test_png_dimensions_are_validated(self):
        self.assertEqual(inspect_icon_png(self.icon.read_bytes()),(32,32))
        for dim in (1,15,513,4096):
            with self.subTest(dim=dim),self.assertRaises(PluginIconError):
                inspect_icon_png(png_bytes(width=dim))

    def test_wrong_hash_does_not_register(self):
        with self.assertRaises(PluginIconError):
            pinned_plugin_icon(self.icon,expected_sha256="0"*64)

    def test_malformed_data_rejected(self):
        raw=self.icon.read_bytes()
        for broken in (b"",b"NOT PNG",raw[:-12],raw[:-6]+b"BADCRC",raw+b"extra"):
            with self.subTest(length=len(broken)),self.assertRaises(PluginIconError):
                inspect_icon_png(broken)

    def test_invalid_hash_argument_rejected(self):
        for expected in (None,"",12,b"a"*64,"G"*64):
            with self.subTest(expected=expected),self.assertRaises(PluginIconError):
                pinned_plugin_icon(self.icon,expected_sha256=expected)

    def test_modified_png_digest_rejected(self):
        self.icon.write_bytes(png_bytes(color=(255,0,0,255)))
        with self.assertRaises(PluginIconError):
            pinned_plugin_icon(self.icon,expected_sha256=self.digest)

    def test_server_and_tools_advertise_pinned_icon(self):
        try:
            import mcp
        except ImportError:
            self.skipTest("MCP SDK unavailable in this environment")
        from sora_commander_next.mcp_adapter import build_server
        from sora_commander_next.host import Host
        host=Host()
        try:
            server=build_server(host,approved_icon_path=self.icon,
                                approved_icon_sha256=self.digest)
            tools=server._tool_manager.list_tools()
            self.assertEqual({t.name for t in tools}, {
                "run_command", "run_command_wait", "await_command_result",
                "read_process_output", "await_process_output", "stop_process",
                "stop_process_wait", "ping", "status", "list_folder",
                "read_image_file", "read_text_file", "review_status", "preview_text_edit",
                "list_sessions", "previous_sessions", "recovery_overview", "request_status", "previous_output",
                # Ten bounded reads, two non-writing previews, local catalog only.
                "list_folder_page", "read_text_page", "find_files", "search_code",
                "path_info", "read_text_lines", "read_many_text_files",
                "compare_text_files", "read_text_chunk", "sha256_file", "git_snapshot", "git_diff", "git_log", "git_show", "git_remote_check", "list_tasks", "run_task", "ogf_cue", "activity",
                "preview_source_create", "preview_source_patch", "tool_catalog",
                "create_directory", "create_file", "move_file", "request_source_patch_dev",
                "list_processes", "get_config", "send_input",
            })
            self.assertFalse({t.name for t in tools} & {
                "approve", "reject", "approve_local", "reject_local",
                "request_source_create", "request_source_patch", "source_review_status",
                "cancel_source_review", "request_text_edit", "request_text_edit_wait",
                "edit_review_status", "await_edit_result", "request_text_create",
                "request_text_create_wait",
            })
            self.assertTrue(all(t.icons for t in tools))
            self.assertTrue(all(t.icons[0].src.startswith("data:image/png;base64,")
                                for t in tools))
            self.assertTrue(server.icons)
        finally:
            host.jobs.close()

    def test_default_server_never_invents_an_icon(self):
        try:
            import mcp
        except ImportError:
            self.skipTest("MCP SDK unavailable in this environment")
        from sora_commander_next.mcp_adapter import build_server
        from sora_commander_next.host import Host
        host=Host()
        try:
            server=build_server(host)
            self.assertFalse(server.icons)
        finally:
            host.jobs.close()


if __name__=="__main__":
    unittest.main()
