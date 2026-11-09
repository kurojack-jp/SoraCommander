"""D06: General repository source CREATE preview, no remote write authority."""

import asyncio
from hashlib import sha256
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
import unittest

from sora_commander_next.file_edits import preview_source_create
from sora_commander_next.file_reads import ReadBoundaryError
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import _validate_arguments, build_server


class SourceCreatePreviewTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
        tmp = self.enterContext(inherited_test_temp())
        self.root = Path(tmp).resolve()
        self.source = self.root / "src"
        self.source.mkdir()
        self.target = self.source / "new_feature.py"
        self.content = "# 日本語 source\nprint('hello')\n"

    def preview(self, **kw):
        return preview_source_create(str(self.target), (str(self.root),),
                                     self.content, **kw)

    def test_unicode_source_diff_digest_and_full_review_without_writing(self):
        self.assertFalse(self.target.exists())
        before = set(self.source.iterdir())
        row = self.preview()
        self.assertEqual(row["status"], "PREVIEW_ONLY")
        self.assertIs(row["creation_authorized"], False)
        self.assertEqual(row["operation"], "create")
        self.assertEqual(row["bytes"], len(self.content.encode("utf-8")))
        self.assertEqual(row["new_sha256"],
                         sha256(self.content.encode("utf-8")).hexdigest())
        self.assertIn("+print('hello')", row["diff"])
        self.assertIn('"operation": "propose creating exactly ONE absent source file"',
                      row["full_escaped_review"])
        self.assertIn("\\u65e5\\u672c\\u8a9e", row["full_escaped_review"])
        self.assertIn("\\n", row["full_escaped_review"])
        self.assertFalse(self.target.exists())
        self.assertEqual(before, set(self.source.iterdir()))

    def test_existing_target_never_overwritten(self):
        self.target.write_text("existing", encoding="utf-8")
        with self.assertRaises(ReadBoundaryError):
            self.preview()
        self.assertEqual(self.target.read_text(encoding="utf-8"), "existing")

    def test_outside_parent_nonexistent_parent_and_symlink_rejected(self):
        with inherited_test_temp() as outside:
            external = Path(outside) / "secret.py"
            with self.assertRaises(ReadBoundaryError):
                preview_source_create(str(external), (str(self.root),), "danger")
            missing = self.source / "not_created" / "new.py"
            with self.assertRaises((ReadBoundaryError, OSError)):
                preview_source_create(str(missing), (str(self.root),), "no")
            alias = self.root / "linked"
            try:
                alias.symlink_to(Path(outside), target_is_directory=True)
                # The inherited fixture refuses links; remove ours first.
                self.addCleanup(alias.unlink)
            except (OSError, NotImplementedError):
                return
            with self.assertRaises(ReadBoundaryError):
                preview_source_create(str(alias / "foo.py"),
                                      (str(self.root),), "no")

    def test_hidden_and_generated_parent_directories_denied(self):
        for name in (".git", ".venv", "node_modules", "BUILD"):
            folder = self.root / name
            folder.mkdir()
            with self.subTest(folder=name), self.assertRaises(ReadBoundaryError):
                preview_source_create(str(folder / "new.py"),
                                      (str(self.root),), "print('no')")
            self.assertFalse((folder / "new.py").exists())

    def test_reservations_and_problematic_file_names_refused(self):
        for name in (".env", "CON.py", "prn.md", "NUL.txt", "COM1.py",
                     "config.py:evil", "source.exe", "bad?.py", "bad*.md"):
            with self.subTest(name=name):
                with self.assertRaises(ReadBoundaryError):
                    preview_source_create(str(self.source / name),
                                          (str(self.root),), "suspicious")
        for bad_content in (None, True, b"raw", "x" * 32769, "あ" * 9000,
                            "\ud800"):
            with self.subTest(content=repr(bad_content)[:70]):
                with self.assertRaises(ReadBoundaryError):
                    preview_source_create(str(self.target), (str(self.root),),
                                          bad_content)

    def test_mcp_real_sdk_and_strict_wire_arguments(self):
        host = Host(policy=Policy(roots=(str(self.root),)))
        try:
            server = build_server(host)
            self.assertIn("preview_source_create",
                          {t.name for t in server._tool_manager.list_tools()})
            _, data = asyncio.run(server.call_tool(
                "preview_source_create",
                {"path": str(self.target), "content": self.content},
            ))
            self.assertEqual(data["status"], "PREVIEW_ONLY")
            self.assertIs(data["creation_authorized"], False)
            self.assertFalse(self.target.exists())
            for payload in ({"path": str(self.target), "content": None},
                            {"path": str(self.target), "content": "ok", "approve": True},
                            {"path": str(self.target) + "\x00", "content": "ok"},
                            {"path": str(self.target), "content": "z" * 32769}):
                with self.subTest(payload=str(payload)[:100]):
                    with self.assertRaises(ValueError):
                        _validate_arguments("preview_source_create", payload)
            tools = {t.name for t in server._tool_manager.list_tools()}
            self.assertNotIn("create_source_file", tools)
            self.assertNotIn("commit_source_create", tools)
        finally:
            host.jobs.close()

    def test_default_host_denies_preview(self):
        from mcp.server.fastmcp.exceptions import ToolError
        host = Host()
        try:
            server = build_server(host)
            with self.assertRaises(ToolError):
                asyncio.run(server.call_tool(
                    "preview_source_create",
                    {"path": str(self.target), "content": self.content},
                ))
            self.assertFalse(self.target.exists())
        finally:
            host.jobs.close()


if __name__ == "__main__":
    unittest.main()
