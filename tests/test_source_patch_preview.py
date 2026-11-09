"""D06 read-only one-anchor source patch preview through FastMCP SDK."""
import asyncio
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sora_commander_next.file_edits import preview_source_patch, StalePreviewError
from sora_commander_next.file_reads import ReadBoundaryError
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import build_server, _validate_arguments
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import mock_read_boundary


class SourcePatchPreviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(inherited_test_temp())).resolve()
        # Option B: production _check denies the canonical temp/ fixture ancestor.
        self.enterContext(mock_read_boundary(
            self.root, ("src/feature.py", "src/raw.py"), preview=True))
        self.src = self.root / "src"
        self.src.mkdir()
        self.target = self.src / "feature.py"
        self.original = "# 日本語\r\nx = 10\r\nprint(x)\r\n"
        self.target.write_bytes(self.original.encode("utf-8"))
        self.old_hash = sha256(self.target.read_bytes()).hexdigest()

    def patch(self, old="x = 10", new="x = 20", *, path=None, digest=None):
        return preview_source_patch(
            str(self.target) if path is None else path,
            (str(self.root),), old, new,
            self.old_hash if digest is None else digest,
        )

    def test_one_anchor_prepares_complete_new_content_and_digest(self):
        before = self.target.read_bytes()
        row = self.patch()
        self.assertEqual(row["status"], "PREVIEW_ONLY")
        self.assertEqual(row["operation"], "replace_literal_once")
        self.assertFalse(row["creation_authorized"])
        self.assertFalse(row["write_authorized"])
        self.assertTrue(row["changed"])
        self.assertIn("-x = 10", row["diff"])
        self.assertIn("+x = 20", row["diff"])
        self.assertIn("x = 20\r\n", row["content"])
        self.assertIn("\\r\\n", row["complete_escaped_review"])
        self.assertEqual(row["old_sha256"], self.old_hash)
        self.assertEqual(row["new_sha256"],
                         sha256(row["content"].encode("utf-8")).hexdigest())
        self.assertEqual(self.target.read_bytes(), before)

    def test_duplicate_anchor_fails_closed_and_never_writes(self):
        self.target.write_bytes(b"x = 10\nx = 10\n")
        fresh = sha256(self.target.read_bytes()).hexdigest()
        with self.assertRaises(ReadBoundaryError):
            self.patch(digest=fresh)
        self.assertEqual(self.target.read_bytes(), b"x = 10\nx = 10\n")

    def test_not_found_stale_input_and_unchanged_proposal(self):
        with self.assertRaises(ReadBoundaryError):
            self.patch(old="not in source")
        with self.assertRaises(StalePreviewError):
            self.patch(digest="0" * 64)
        row = self.patch(new="x = 10")
        self.assertEqual(row["new_sha256"], self.old_hash)
        self.assertFalse(row["changed"])
        self.assertEqual(self.target.read_bytes(), self.original.encode("utf-8"))

    def test_bad_types_digest_unicode_and_oversize(self):
        for anchor, new, digest in (
            ("", "new", self.old_hash),
            (None, "new", self.old_hash),
            (True, "new", self.old_hash),
            ("x = 10", None, self.old_hash),
            ("x = 10", b"broken", self.old_hash),
            ("x = 10", "\ud800", self.old_hash),
            ("x = 10", "Z" * 4100, self.old_hash),
            ("Z" * 4100, "x", self.old_hash),
            ("x = 10", "new", "A" * 64),
            ("x = 10", "new", True),
        ):
            with self.subTest(anchor=repr(anchor)[:18],new=repr(new)[:18],digest=repr(digest)[:12]):
                with self.assertRaises(ReadBoundaryError):
                    self.patch(old=anchor, new=new, digest=digest)
        self.assertEqual(self.target.read_bytes(), self.original.encode("utf-8"))

    def test_outside_scope_binary_and_hidden_code_denied(self):
        with TemporaryDirectory(prefix="scn-source-patch-other-") as outside:
            foreign = Path(outside) / "evil.py"
            foreign.write_bytes(self.target.read_bytes())
            with self.assertRaises(ReadBoundaryError):
                self.patch(path=str(foreign))
        secret = self.root / ".git"
        secret.mkdir()
        private = secret / "status.py"
        private.write_bytes(self.target.read_bytes())
        with self.assertRaises(ReadBoundaryError):
            self.patch(path=str(private))
        binary = self.src / "raw.py"
        binary.write_bytes(b"\xffx = 10")
        with self.assertRaises(ReadBoundaryError):
            self.patch(path=str(binary),digest=sha256(binary.read_bytes()).hexdigest())

    def test_real_mcp_sdk_returns_prepared_content_without_write(self):
        host = Host(policy=Policy(roots=(str(self.root),)))
        try:
            sdk = build_server(host)
            self.assertIn("preview_source_patch",
                          {t.name for t in sdk._tool_manager.list_tools()})
            _, data = asyncio.run(sdk.call_tool(
                "preview_source_patch", {
                    "path": str(self.target), "old_text": "x = 10",
                    "new_text": "x = 20",
                    "expected_sha256": self.old_hash,
                }))
            self.assertEqual(data["status"], "PREVIEW_ONLY")
            self.assertFalse(data["write_authorized"])
            # The AI-facing preview carries the digest and complete diff, never
            # the whole proposed source (up to the 64 KiB writer cap).
            self.assertNotIn("content", data)
            self.assertIn("+x = 20", data["diff"])
            self.assertEqual(data["new_sha256"], sha256(
                self.original.replace("x = 10", "x = 20").encode("utf-8")).hexdigest())
            self.assertEqual(self.target.read_bytes(), self.original.encode("utf-8"))
            for args in (
                {"old_text": None},
                {"old_text": ""},
                {"new_text": True},
                {"expected_sha256": "bad"},
                {"path": str(self.target) + "\x00"},
                {"approve": True},
            ):
                request = {"path": str(self.target), "old_text": "x = 10",
                           "new_text": "x = 20", "expected_sha256": self.old_hash, **args}
                with self.subTest(args=repr(args)), self.assertRaises(ValueError):
                    _validate_arguments("preview_source_patch", request)
        finally:
            host.jobs.close()

    def write_target(self, text):
        self.target.write_bytes(text.encode("utf-8"))
        self.old_hash = sha256(self.target.read_bytes()).hexdigest()
        return text

    def test_source_above_review_window_but_within_writer_cap_previews(self):
        text = self.write_target("".join(
            f"# filler line {n:05d}\n" for n in range(1200)) + "x = 10\n")
        self.assertGreater(len(text.encode("utf-8")), 16384)
        row = self.patch()
        self.assertEqual(row["status"], "PREVIEW_ONLY")
        self.assertEqual(row["content"], text.replace("x = 10", "x = 20"))
        self.assertLess(len(row["diff"].encode("utf-8")), 1024)
        self.assertFalse(row["write_authorized"])
        self.assertEqual(self.target.read_bytes(), text.encode("utf-8"))

    def test_source_or_result_above_writer_cap_rejected(self):
        filler = "#" * 63 + "\n"
        self.write_target(filler * 1024 + "x = 10\n")
        with self.assertRaises(ReadBoundaryError):
            self.patch()
        self.write_target(filler * 1023 + "#" * 50 + "\n" + "x = 10\n")
        self.assertLessEqual(self.target.stat().st_size, 65536)
        with self.assertRaises(ReadBoundaryError):
            self.patch(new="x = " + "2" * 64)

    def test_diff_above_review_cap_still_rejected(self):
        self.write_target("y = '" + "Z" * 17000 + "'\nx = 10\n")
        with self.assertRaises(ReadBoundaryError):
            self.patch()

    def test_empty_read_roots_denied(self):
        from mcp.server.fastmcp.exceptions import ToolError
        host = Host()
        try:
            with self.assertRaises(ToolError):
                asyncio.run(build_server(host).call_tool(
                    "preview_source_patch", {
                        "path": str(self.target), "old_text": "x = 10",
                        "new_text": "x = 20", "expected_sha256": self.old_hash,
                    }))
        finally:
            host.jobs.close()


if __name__ == "__main__":
    unittest.main()
