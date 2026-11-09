"""Offline-only >64KiB output persistence/recovery without launching a job."""
import base64
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import canonical_fixture_admission
import unittest
from unittest.mock import patch

from sora_commander_next.offline_output_spool import (
    SPOOL_NAME, OutputSpoolError, OutputSpoolFull, append_dev_output,
    create_dev_spool, read_dev_output_page,
)


class OfflineSpoolTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(canonical_fixture_admission())
        t = self.enterContext(inherited_test_temp())
        self.root = Path(t).resolve()
        self.path = self.root / SPOOL_NAME
        self.roots = (str(self.root),)
        create_dev_spool(str(self.path), self.roots)

    def append(self, stream, data):
        return append_dev_output(str(self.path), self.roots, stream, data)

    def page(self, **kwargs):
        return read_dev_output_page(str(self.path), self.roots, **kwargs)

    def test_over_64k_stdout_stderr_recovered_in_pages(self):
        expected = []
        for i in range(60):
            kind = "stdout" if i % 3 else "stderr"
            data = (f"record-{i:04d} 日本語\n".encode("utf-8") + b"x" * 2000)
            expected.append((kind, data))
            self.append(kind, data)
        raw = b"".join(data for _, data in expected)
        self.assertGreater(len(raw), 65536)
        self.assertEqual(self.page()["captured_bytes"], len(raw))
        cursor, seen = 0, bytearray()
        fragments = []
        for _ in range(100):
            page = self.page(offset=cursor, max_bytes=2048)
            self.assertEqual(page["offset"], cursor)
            self.assertEqual(page["status"], "DATA_ONLY_UNVERIFIED")
            for chunk in page["sections"]:
                payload = base64.b64decode(chunk["data_base64"], validate=True)
                fragments.append((chunk["stream"], payload))
                seen.extend(payload)
            cursor = page["next_offset"]
            if not page["truncated"]:
                break
        self.assertEqual(bytes(seen), raw)
        self.assertEqual(cursor, len(raw))
        self.assertEqual(self.page(offset=cursor)["sections"], [])
        self.assertFalse(self.page(offset=cursor)["truncated"])
        self.assertIn("stderr", {kind for kind, data in fragments})
        self.assertIn("stdout", {kind for kind, data in fragments})

    def test_reopen_after_append_recovers_previous_bytes(self):
        self.append("stdout", b"initial")
        a = self.page()
        self.assertEqual(base64.b64decode(a["sections"][0]["data_base64"]), b"initial")
        self.append("stderr", b"later warning")
        b = self.page(offset=a["next_offset"])
        self.assertEqual(b["sections"][0]["stream"], "stderr")
        self.assertEqual(base64.b64decode(b["sections"][0]["data_base64"]),
                         b"later warning")
        self.assertEqual(b["next_offset"], len(b"initiallater warning"))
        self.assertNotIn("exit_code", b)
        self.assertNotIn("COMPLETED", repr(b))

    def test_append_full_never_truncates_or_overwrites_existing_capture(self):
        self.append("stdout", b"0123456789")
        saved = self.path.read_bytes()
        with patch("sora_commander_next.offline_output_spool.MAX_CAPTURE", 12):
            with self.assertRaises(OutputSpoolFull):
                self.append("stderr", b"not allowed")
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertEqual(self.page()["captured_bytes"], 10)

    def test_partial_tail_and_crc_corruption_are_unknown_not_result(self):
        self.append("stdout", b"hello")
        stable = self.path.read_bytes()
        with self.path.open("ab") as output:
            output.write(b"\x01\x00\x00")
        with self.assertRaises(OutputSpoolError):
            self.page()
        self.path.write_bytes(stable)
        corrupted = bytearray(self.path.read_bytes())
        corrupted[-1] ^= 1
        self.path.write_bytes(corrupted)
        with self.assertRaises(OutputSpoolError):
            self.page()
        with self.assertRaises(OutputSpoolError):
            self.append("stderr", b"cannot conceal corruption")

    def test_path_and_types_are_bounded(self):
        with self.assertRaises(OutputSpoolError):
            create_dev_spool(str(self.path), self.roots)
        with self.assertRaises(OutputSpoolError):
            create_dev_spool(str(self.root / "other.spool"), self.roots)
        for stream, data in (
            ("stderr", b""), ("stdout", b"x" * 4097), ("log", b"x"),
            ("stdout", "not-bytes"), (True, b"a"),
        ):
            with self.subTest(stream=stream, length=len(data)):
                with self.assertRaises(OutputSpoolError):
                    self.append(stream, data)
        for offset in (True, -1, "0", 16 * 1024 * 1024 + 1):
            with self.subTest(offset=offset), self.assertRaises(OutputSpoolError):
                self.page(offset=offset)
        for size in (0, True, None, 4097):
            with self.subTest(size=size), self.assertRaises(OutputSpoolError):
                self.page(max_bytes=size)

    def test_scope_symlink_and_hardlink_are_denied(self):
        with inherited_test_temp() as outside:
            foreign = Path(outside) / SPOOL_NAME
            foreign.write_bytes(self.path.read_bytes())
            with self.assertRaises(OutputSpoolError):
                read_dev_output_page(str(foreign), self.roots)
            alias = self.root / "alias"
            try:
                alias.symlink_to(Path(outside), target_is_directory=True)
                # The inherited fixture refuses links; remove ours first.
                self.addCleanup(alias.unlink)
            except (OSError, NotImplementedError):
                pass
            else:
                with self.assertRaises(OutputSpoolError):
                    read_dev_output_page(str(alias / SPOOL_NAME), self.roots)
        self.path.unlink()
        with inherited_test_temp() as outside:
            original = Path(outside) / SPOOL_NAME
            original.write_bytes(b"SCNSPL01")
            try:
                import os
                os.link(original, self.path)
            except (OSError, NotImplementedError):
                self.skipTest("hardlink not supported")
            try:
                with self.assertRaises(OutputSpoolError):
                    self.page()
            finally:
                # Drop our hardlink before the outside fixture is cleaned up.
                self.path.unlink()

    def test_no_job_spawn_or_service_side_effect(self):
        from unittest.mock import patch as patched
        with patched("subprocess.Popen") as spawn, patched("subprocess.run") as run:
            self.append("stdout", b"no execution")
            self.page()
            spawn.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
