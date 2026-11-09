"""Real P2 preview reads with mocked path admission; no policy acceptance."""
import difflib
from hashlib import sha256
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import mock_read_boundary
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sora_commander_next.file_edits import preview_text_edit, StalePreviewError
from sora_commander_next.file_reads import ReadBoundaryError


class PreviewBoundedReadTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(inherited_test_temp())).resolve()
        self.path = self.root / 'preview.txt'
        self.enterContext(mock_read_boundary(self.root, (self.path.name,), preview=True))
        self.original = b'before\r\n'
        self.path.write_bytes(self.original)
        self.roots = (str(self.root),)

    def preview(self, content='after\n', digest=None):
        if digest is None:
            digest = sha256(self.original).hexdigest()
        return preview_text_edit(str(self.path), self.roots, content, digest)

    def test_oversize_rejected_before_open_or_read_bytes(self):
        self.path.write_bytes(b'x' * 65537)
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('unbounded read')), \
                patch.object(Path, 'open', side_effect=AssertionError('opened oversize')):
            with self.assertRaises(ReadBoundaryError):
                self.preview()

    def test_single_read_is_bounded_even_if_file_grows(self):
        real_open = Path.open
        reads = []

        def open_growing(path, *args, **kwargs):
            source = real_open(path, *args, **kwargs)
            original_read = source.read

            def read(size):
                reads.append(size)
                # Model growth after the pre-read fstat without filesystem races.
                original_read(size)
                return b'x' * size

            source.read = read
            return source

        with patch.object(Path, 'open', open_growing), \
                patch.object(Path, 'read_bytes', side_effect=AssertionError('unbounded read')):
            with self.assertRaises(ReadBoundaryError):
                self.preview()
        self.assertEqual(reads, [65537])

    def test_hardlink_metadata_rejected_before_open(self):
        real_stat = Path.stat

        def linked_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == self.path:
                return SimpleNamespace(st_mode=info.st_mode, st_nlink=2,
                                       st_size=info.st_size)
            return info

        # Simulated metadata avoids privileged/native hardlink fixture work.
        with patch.object(Path, 'stat', linked_stat), \
                patch.object(Path, 'open', side_effect=AssertionError('opened hardlink')):
            with self.assertRaises(ReadBoundaryError):
                self.preview()

    def test_directory_rejected_before_open(self):
        with patch.object(Path, 'open', side_effect=AssertionError('opened directory')):
            with self.assertRaises(ReadBoundaryError):
                preview_text_edit(str(self.root), self.roots, 'after', '0' * 64)

    def test_stale_sha_refused_before_diff(self):
        with patch('sora_commander_next.file_edits.difflib.unified_diff',
                   side_effect=AssertionError('generated stale diff')):
            with self.assertRaises(StalePreviewError):
                self.preview(digest='0' * 64)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_malformed_utf8_rejected(self):
        self.path.write_bytes(b'\xff')
        with self.assertRaises(ReadBoundaryError):
            self.preview(digest=sha256(b'\xff').hexdigest())
        self.assertEqual(self.path.read_bytes(), b'\xff')

    def test_named_file_identity_change_rejected(self):
        real_stat = Path.stat
        changed = False

        def changed_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if changed and path == self.path:
                return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                                       st_dev=info.st_dev, st_ino=info.st_ino + 1,
                                       st_size=info.st_size, st_mtime_ns=info.st_mtime_ns)
            return info

        import os
        real_fstat = os.fstat
        calls = 0

        def fstat(fd):
            nonlocal calls, changed
            info = real_fstat(fd)
            calls += 1
            changed = calls == 2
            return info

        with patch.object(Path, 'stat', changed_stat), patch('os.fstat', fstat):
            with self.assertRaisesRegex(ReadBoundaryError, 'file identity changed'):
                self.preview()
        self.assertEqual(calls, 2)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_valid_preview_exact_contract_without_read_bytes(self):
        proposed = 'after\n'
        expected_diff = ''.join(difflib.unified_diff(
            ['before\r\n'], [proposed], fromfile='current', tofile='proposed'))
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('unbounded read')):
            result = self.preview(proposed)
        self.assertEqual(result, {
            'path': str(self.path), 'old_sha256': sha256(self.original).hexdigest(),
            'new_sha256': sha256(proposed.encode('utf-8')).hexdigest(),
            'changed': True, 'diff': expected_diff, 'diff_truncated': False,
        })
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_unchanged_preview(self):
        result = self.preview(self.original.decode('utf-8'))
        self.assertFalse(result['changed'])
        self.assertEqual(result['diff'], '')
        self.assertFalse(result['diff_truncated'])
        self.assertEqual(result['old_sha256'], result['new_sha256'])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_exact_limit_and_diff_truncation(self):
        self.original = b'a' * 65536
        self.path.write_bytes(self.original)
        proposed = 'b' * 65536
        full_diff = ''.join(difflib.unified_diff(
            [self.original.decode('utf-8')], [proposed],
            fromfile='current', tofile='proposed'))
        result = self.preview(proposed)
        self.assertTrue(result['changed'])
        self.assertEqual(result['diff'], full_diff[:32768])
        self.assertTrue(result['diff_truncated'])
        self.assertEqual(result['new_sha256'], sha256(proposed.encode('utf-8')).hexdigest())
        self.assertEqual(self.path.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main(failfast=True)
