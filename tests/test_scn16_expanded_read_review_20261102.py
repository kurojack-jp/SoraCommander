"""SCN16: allowed-root controls and synthetic names, no temporary fixtures.

Only this source file is read. Directory discovery uses an explicit fake entry
list; excluded names must be filtered before any filesystem access to them.
"""
import asyncio
from hashlib import sha256
from pathlib import Path
import unittest
from unittest.mock import patch

from sora_commander_next import file_reads as reads
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import build_server
from mcp.server.fastmcp.exceptions import ToolError


SOURCE = Path(__file__).resolve()
ROOT = SOURCE.parent
MARKER = "SCN16_ALLOWED_SOURCE_CONTROL"
BLOCKED = ("start-sora-commander.cmd", "START-SORA-COMMANDER.CMD",
           "synthetic.user.txt", "SYNTHETIC.USER.TXT",
           "state", "runtime", "logs", "temp")


class ExpandedReadReviewTests(unittest.TestCase):
    def setUp(self):
        self.roots = (str(ROOT),)

    def direct_routes(self, path):
        return (
            ("read_text_file", {"path": path}),
            ("read_text_page", {"path": path}),
            ("read_text_lines", {"path": path}),
            ("read_text_chunk", {"path": path}),
            ("path_info", {"path": path}),
            ("sha256_file", {"path": path}),
            ("read_many_text_files", {"paths": [str(SOURCE), path]}),
            ("compare_text_files", {"before_path": str(SOURCE), "after_path": path}),
        )

    def test_allowed_root_positive_controls_for_all_direct_routes(self):
        raw = SOURCE.read_bytes()
        digest = sha256(raw).hexdigest()
        for name, arguments in self.direct_routes(str(SOURCE)):
            if name == "read_many_text_files":
                arguments = {"paths": [str(SOURCE)]}
            with self.subTest(route=name):
                result = getattr(reads, name)(roots=self.roots, **arguments)
                if name in ("read_text_file", "read_text_page", "read_text_lines",
                            "path_info", "sha256_file"):
                    self.assertEqual(result["sha256"], digest)
                elif name == "read_text_chunk":
                    self.assertIn(MARKER, result["content"])
                elif name == "read_many_text_files":
                    self.assertEqual(result["files"][0]["sha256"], digest)
                else:
                    self.assertFalse(result["changed"])

    def test_blocked_names_fail_before_resolution_in_all_direct_routes(self):
        # The safe file and root may resolve; synthetic excluded paths may not.
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            if path not in (SOURCE, ROOT):
                self.fail("excluded synthetic path reached filesystem resolution")
            return original(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve):
            for component in BLOCKED:
                for name, arguments in self.direct_routes(str(ROOT / component)):
                    with self.subTest(component=component, route=name):
                        with self.assertRaises(reads.ReadBoundaryError):
                            getattr(reads, name)(roots=self.roots, **arguments)

    def test_sensitive_root_and_descendant_cannot_grant_access(self):
        with patch.object(Path, "resolve", side_effect=AssertionError(
                "sensitive synthetic path must be rejected lexically")):
            for name in BLOCKED:
                root = ROOT / name
                for target in (root, root / "ordinary.py"):
                    with self.subTest(name=name, target=target.name):
                        with self.assertRaises(reads.ReadBoundaryError):
                            reads._check(str(target), (str(root),))

    def test_discovery_filters_before_touching_synthetic_entries(self):
        entries = [ROOT / name for name in BLOCKED] + [SOURCE]
        def iterdir(path):
            self.assertEqual(path, ROOT)
            return iter(entries)
        original_stat = Path.stat
        def stat(path, *args, **kwargs):
            if any(part in BLOCKED for part in path.parts):
                self.fail("excluded synthetic entry reached metadata access")
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, "iterdir", iterdir), patch.object(Path, "stat", stat):
            for depth in (0, 1):
                result = reads.list_folder(str(ROOT), self.roots, depth=depth)
                self.assertEqual([e["name"] for e in result["entries"]], [SOURCE.name])
            page = reads.list_folder_page(str(ROOT), self.roots, limit=1)
            self.assertEqual(page["total_entries"], 1)
            self.assertFalse(page["truncated"])
            found = reads.find_files(str(ROOT), self.roots, name="scn16")
            self.assertEqual([e["path"] for e in found["matches"]], [SOURCE.name])
            result = reads.search_code(str(ROOT), self.roots, text=MARKER)
            self.assertGreater(result["count"], 0)
            self.assertEqual(result["scanned_files"], 1)
            self.assertTrue(all(e["path"] == SOURCE.name for e in result["matches"]))

    def test_real_sdk_direct_routes_positive_and_blocked(self):
        host = Host(policy=Policy(roots=self.roots))
        self.addCleanup(host.jobs.close)
        server = build_server(host)
        for name, arguments in self.direct_routes(str(SOURCE)):
            if name == "read_many_text_files":
                arguments = {"paths": [str(SOURCE)]}
            with self.subTest(route=name, allowed=True):
                self.assertTrue(asyncio.run(server.call_tool(name, arguments)))
        for component in BLOCKED:
            for name, arguments in self.direct_routes(str(ROOT / component)):
                with self.subTest(route=name, component=component):
                    with self.assertRaises(ToolError):
                        asyncio.run(server.call_tool(name, arguments))


if __name__ == "__main__":
    unittest.main()
