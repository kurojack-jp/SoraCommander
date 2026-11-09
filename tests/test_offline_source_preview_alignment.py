"""Offline contract alignment of existing source preview with data-only digest candidate."""
import unittest
from hashlib import sha256
from pathlib import Path
from sora_commander_next.file_edits import preview_source_patch
from sora_commander_next.offline_source_patch_candidate import inspect_patch_bytes
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import mock_read_boundary

class PreviewCandidateAlignment(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(inherited_test_temp())).resolve()
        self.source=self.root/"sample.py"
        # Option B: production _check denies the canonical temp/ fixture ancestor.
        # Registered last, so the mock unwinds before guarded fixture cleanup.
        self.enterContext(mock_read_boundary(self.root,("sample.py",),preview=True))
    def test_crlf_utf8_source_one_anchor(self):
        root,source=self.root,self.source
        original="# 日本語\r\nx = 1\r\n"
        source.write_bytes(original.encode("utf-8"))
        before=source.read_bytes()
        digest=sha256(before).hexdigest()
        preview=preview_source_patch(str(source),(str(root),),"x = 1","x = 2",digest)
        candidate=inspect_patch_bytes(before,preview["content"].encode("utf-8"),digest)
        self.assertEqual(candidate.new_sha256,preview["new_sha256"])
        self.assertEqual(candidate.old_sha256,preview["old_sha256"])
        self.assertTrue(candidate.changed)
        self.assertEqual(candidate.authority,"NONE_PREVIEW_ONLY")
        self.assertFalse(preview["write_authorized"])
        self.assertEqual(source.read_bytes(),before)
    def test_noop_preview_never_authorizes(self):
        root,source=self.root,self.source
        source.write_bytes(b"x = 1\n")
        before=source.read_bytes()
        digest=sha256(before).hexdigest()
        preview=preview_source_patch(str(source),(str(root),),"x = 1","x = 1",digest)
        candidate=inspect_patch_bytes(before,preview["content"].encode("utf-8"),digest)
        self.assertFalse(candidate.changed)
        self.assertFalse(preview["write_authorized"])
        self.assertEqual(source.read_bytes(),before)
