"""Isolated synthetic ZIP wheels; never install or execute payloads."""
import base64
from dataclasses import FrozenInstanceError
import hashlib
import io
import csv
import os
from pathlib import Path
import stat
import struct
import sys
import unittest
from unittest.mock import patch
import warnings
import zipfile

from tests._inherited_test_temp import inherited_test_temp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sora_commander_next import release_wheel_payload_audit as audit


DIST = "demo-1.0.dist-info"
RECORD = DIST + "/RECORD"


def hashed(data):
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def record_bytes(rows):
    stream = io.StringIO(newline="")
    csv.writer(stream).writerows(rows)
    return stream.getvalue().encode("utf-8")


class WheelPayloadTests(unittest.TestCase):
    def setUp(self):
        # Disposable fake wheel bytes only. Inventory assertions must observe
        # this fixture, not unrelated activity in the shared Windows TEMP.
        self.root = Path(self.enterContext(inherited_test_temp()))
        self.wheel = self.root / "demo-1.0-1-py3-none-any.whl"
        self.wheel.touch(exist_ok=False)

    def build(self, files=None, *, rows=None, raw_record=None,
              compression=zipfile.ZIP_STORED, force_zip64=False):
        if files is None:
            files = [("demo/__init__.py", b"raise RuntimeError('NEVER EXECUTE')\n"),
                     (DIST + "/METADATA", b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"),
                     (DIST + "/WHEEL", b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")]
        if rows is None:
            rows = [(n.filename if isinstance(n, zipfile.ZipInfo) else n,
                     hashed(data), str(len(data))) for n, data in files]
            rows.append((RECORD, "", ""))
        if raw_record is None:
            raw_record = record_bytes(rows)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.wheel, "w", compression=compression) as archive:
                for name, data in [*files, (RECORD, raw_record)]:
                    if force_zip64:
                        with archive.open(name, "w", force_zip64=True) as stream:
                            stream.write(data)
                    else:
                        archive.writestr(name, data)
        return self.wheel

    def run_audit(self, path=None, **overrides):
        path = self.wheel if path is None else path
        data = self.wheel.read_bytes()
        args = dict(expected_sha256=hashlib.sha256(data).hexdigest(), expected_size=len(data))
        args.update(overrides)
        return audit.audit_wheel_payload(path, **args)

    def reject(self, **overrides):
        with self.assertRaises(audit.WheelPayloadAuditError):
            self.run_audit(**overrides)

    def test_positive_native_immutable_no_authority_or_extraction(self):
        self.build([("demo/ext.cp313-win_amd64.pyd", b"not executable"),
                    ("demo/native.DLL", b"not a real dll"),
                    ("demo/data,quoted.txt", b"csv comma"),
                    ("demo/日本語.txt", b"utf8"),
                    ("demo/empty", b"")])
        before_names = set(self.root.iterdir())
        before_bytes = self.wheel.read_bytes()
        with patch.object(zipfile.ZipFile, "extract", side_effect=AssertionError), patch.object(
                zipfile.ZipFile, "extractall", side_effect=AssertionError):
            report = self.run_audit()
        self.assertEqual(report.archive_sha256, hashlib.sha256(self.wheel.read_bytes()).hexdigest())
        self.assertEqual(report.archive_size, self.wheel.stat().st_size)
        self.assertEqual(report.entry_count, 6)
        self.assertEqual(report.native_module_files,
                         ("demo/ext.cp313-win_amd64.pyd", "demo/native.DLL"))
        self.assertEqual(report.record_coverage_verdict, "PASS")
        self.assertEqual(report.status, "HOLD_LIVE")
        for name in ("install_permitted", "publisher_authenticated", "runtime_authorized"):
            self.assertIs(getattr(report, name), False)
            with self.assertRaises(FrozenInstanceError):
                setattr(report, name, True)
        self.assertEqual(before_names, set(self.root.iterdir()))
        self.assertEqual(before_bytes, self.wheel.read_bytes())

    def test_standard_record_empty_self_and_deflate(self):
        self.build(compression=zipfile.ZIP_DEFLATED)
        self.assertEqual(self.run_audit().entry_count, 4)

    def test_bounded_local_force_zip64_is_valid(self):
        self.build(force_zip64=True)
        self.assertEqual(self.run_audit().record_coverage_verdict, "PASS")

    def test_standard_data_descriptor_is_valid(self):
        self.build(compression=zipfile.ZIP_DEFLATED)
        # Repack into a genuinely nonseekable writer, which emits descriptors
        # and zero local CRC/size fields as allowed by standard ZIP writers.
        class Nonseekable(io.BytesIO):
            def seekable(self):
                return False

            def seek(self, *args):
                raise io.UnsupportedOperation("nonseekable fixture")

        stream = Nonseekable()
        with zipfile.ZipFile(self.wheel, "r") as original, zipfile.ZipFile(
                stream, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for info in original.infolist():
                output.writestr(info.filename, original.read(info))
        self.wheel.write_bytes(stream.getvalue())
        self.assertEqual(self.run_audit().record_coverage_verdict, "PASS")

    def test_build_tag_and_normalized_dist_info(self):
        self.build()
        moved = self.wheel.with_name(self.wheel.name.replace("demo-1.0-1", "demo-1.0-2", 1))
        self.wheel.rename(moved)
        self.wheel = moved
        self.assertEqual(self.run_audit().record_coverage_verdict, "PASS")

    def test_strict_hash_pins_and_wrong_digest(self):
        self.build()
        for value in (None, 1, b"0" * 64, "0" * 63, "A" * 64, "g" * 64, "0" * 64,
                      hashlib.sha256(self.wheel.read_bytes()).hexdigest() + "\n"):
            with self.subTest(value=value):
                self.reject(expected_sha256=value)

    def test_strict_size_pins(self):
        self.build()
        for value in (True, None, -1, 0, 1, self.wheel.stat().st_size + 1,
                      audit.MAX_ARCHIVE_BYTES + 1):
            with self.subTest(value=value):
                self.reject(expected_size=value)

    def test_path_lexical_denials(self):
        self.build()
        for value in ("demo.whl", "C:demo.whl", "\\\\server\\share\\demo.whl",
                      "\\\\?\\C:\\demo.whl", "\\??\\C:\\demo.whl", "/tmp/demo.whl",
                      str(self.root) + "\\..\\demo.whl", str(self.root) + "\\.\\demo.whl",
                      str(self.wheel) + ":ads", str(self.wheel) + "\x00", 12,
                      str(self.root) + "\\bad.\\demo.whl"):
            with self.subTest(value=value):
                self.reject(path=value)

    def test_nonfixed_drive_denied(self):
        self.build()
        with patch.object(audit, "_fixed_drive", return_value=False):
            self.reject()

    def test_extension_and_filename_denied(self):
        self.build()
        for name in ("demo.zip", "demo.whl", "demo-1-py3-none-any.WHL", "demo-1-no-py3-none-any.whl"):
            with self.subTest(name=name):
                self.reject(path=self.root / name)

    def test_hardlink_rejected(self):
        self.build()
        other = self.wheel.with_suffix(".link")
        os.link(self.wheel, other)
        self.addCleanup(lambda: other.unlink(missing_ok=True))
        self.reject()

    def test_reparse_ancestor_mock_denied_before_read(self):
        self.build()
        original = Path.lstat

        class Reparse:
            st_mode = stat.S_IFDIR | 0o700
            st_file_attributes = 0x400

        def lstat(path, *args, **kwargs):
            return Reparse() if path == self.root else original(path, *args, **kwargs)

        with patch.object(Path, "lstat", lstat):
            self.reject()

    def test_symlink_file_metadata_mock_denied(self):
        self.build()
        original = Path.lstat

        class Link:
            st_mode = stat.S_IFLNK | 0o700

        def lstat(path, *args, **kwargs):
            return Link() if path == self.wheel else original(path, *args, **kwargs)

        with patch.object(Path, "lstat", lstat):
            self.reject()

    def test_nonregular_local_file_denied(self):
        self.build()
        original = Path.lstat

        class Pipe:
            st_mode = stat.S_IFIFO | 0o600

        def lstat(path, *args, **kwargs):
            return Pipe() if path == self.wheel else original(path, *args, **kwargs)

        with patch.object(Path, "lstat", lstat):
            self.reject()

    def test_duplicate_and_casefold_zip_members(self):
        for names in (("a", "a"), ("demo/File", "demo/file"), ("demo/SS", "demo/ß")):
            with self.subTest(names=names):
                self.build([(n, b"a") for n in names])
                self.reject()

    def test_unsafe_member_names(self):
        for name in ("../evil", "demo/../evil", "demo/./evil", "/absolute", "C:/evil",
                     "demo\\evil", "demo/data:ads", "demo//evil", "demo/NUL.txt",
                     "demo/trailing. /x", "demo/trailing./x", "demo/*"):
            with self.subTest(name=name):
                self.build([(name, b"data")])
                self.reject()

    def test_null_member_name_not_silently_truncated(self):
        self.build([("demo/xxxx", b"data")])
        self.wheel.write_bytes(self.wheel.read_bytes().replace(b"demo/xxxx", b"demo/x\x00xx"))
        self.reject()

    def test_symlink_directory_fifo_members(self):
        for name, mode in (("demo/link", stat.S_IFLNK | 0o777),
                           ("demo/", stat.S_IFDIR | 0o755),
                           ("demo/fifo", stat.S_IFIFO | 0o600)):
            with self.subTest(name=name):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                self.build([(info, b"target")])
                self.reject()

    def test_encrypted_flag_mock(self):
        self.build()
        original = zipfile.ZipFile.infolist

        def encrypted(archive):
            infos = original(archive)
            infos[0].flag_bits |= 1
            return infos

        with patch.object(zipfile.ZipFile, "infolist", encrypted):
            self.reject()

    def test_encrypted_and_local_flag_mismatch_bytes(self):
        for both in (False, True):
            with self.subTest(both=both):
                self.build()
                data = bytearray(self.wheel.read_bytes())
                struct.pack_into("<H", data, 6, 1)
                if both:
                    central = data.index(b"PK\x01\x02")
                    struct.pack_into("<H", data, central + 8, 1)
                self.wheel.write_bytes(data)
                self.reject()

    def test_zip_bomb_ratio(self):
        self.build([("demo/bomb", b"a" * 100_000)], compression=zipfile.ZIP_DEFLATED)
        self.reject()

    def test_bad_crc_with_fresh_outer_hash(self):
        self.build([("demo/data", b"UNIQUE_PAYLOAD")])
        data = self.wheel.read_bytes().replace(b"UNIQUE_PAYLOAD", b"CHANGEDPAYLOAD")
        self.wheel.write_bytes(data)
        self.reject()

    def test_actual_deflate_size_beyond_forged_metadata(self):
        payload = b"abcde" * 30
        self.build([("demo/data", payload)], compression=zipfile.ZIP_DEFLATED)
        data = bytearray(self.wheel.read_bytes())
        central = data.index(b"PK\x01\x02")
        # ZipExtFile would clip to 3 and accept the matching prefix CRC.
        for offset in (14, central + 16):
            struct.pack_into("<I", data, offset, zipfile.crc32(payload[:3]))
        for offset in (22, central + 24):
            struct.pack_into("<I", data, offset, 3)
        self.wheel.write_bytes(data)
        self.reject()

    def test_invalid_utf8_record(self):
        self.build(raw_record=b"\xff\xfe")
        self.reject()

    def test_invalid_utf8_zip_name(self):
        self.build([("demo/name", b"data")])
        self.wheel.write_bytes(self.wheel.read_bytes().replace(b"demo/name", b"demo/nam\xff"))
        self.reject()

    def test_limits_and_limit_types(self):
        self.build()
        for args in ({"max_entries": 1}, {"max_uncompressed": 1},
                     {"max_entries": True}, {"max_entries": 0},
                     {"max_entries": audit.MAX_ENTRIES + 1},
                     {"max_uncompressed": None}, {"max_uncompressed": 0},
                     {"max_uncompressed": audit.MAX_UNCOMPRESSED + 1}):
            with self.subTest(args=args):
                self.reject(**args)

    def test_record_memory_bound(self):
        self.build(raw_record=b"x" * (audit.MAX_RECORD_BYTES + 1))
        self.reject()

    def test_duplicate_record_rows(self):
        rows = [("demo/a", hashed(b"a"), "1"), ("demo/a", hashed(b"a"), "1"), (RECORD, "", "")]
        self.build([("demo/a", b"a")], rows=rows)
        self.reject()

    def test_missing_record_file_row_or_record_self_row(self):
        for rows in ([(RECORD, "", "")], [("demo/a", hashed(b"a"), "1")]):
            with self.subTest(rows=rows):
                self.build([("demo/a", b"a")], rows=rows)
                self.reject()

    def test_forged_missing_hash_or_wrong_size(self):
        for digest, size in (("", ""), ("", "1"), (hashed(b"a"), ""),
                             (hashed(b"b"), "1"), (hashed(b"a"), "2"),
                             (hashed(b"a"), "-1"), (hashed(b"a"), "True")):
            with self.subTest(digest=digest, size=size):
                self.build([("demo/a", b"a")], rows=[("demo/a", digest, size), (RECORD, "", "")])
                self.reject()

    def test_unsupported_record_digest_or_padding(self):
        for digest in ("sha512=" + "a" * 86, "md5=" + "a" * 22,
                       "sha1=" + "a" * 27, hashed(b"a") + "=", "SHA256=" + "a" * 43):
            with self.subTest(digest=digest):
                self.build([("demo/a", b"a")], rows=[("demo/a", digest, "1"), (RECORD, "", "")])
                self.reject()

    def test_unlisted_or_wrong_case_record_path(self):
        for name in ("unlisted", "../demo/a", "demo/A"):
            with self.subTest(name=name):
                self.build([("demo/a", b"a")], rows=[(name, hashed(b"a"), "1"), (RECORD, "", "")])
                self.reject()

    def test_record_csv_malformed_and_wrong_columns(self):
        for raw in (b'"unterminated', b"a,b,c,d\n", b"\n", b"a,b\n"):
            with self.subTest(raw=raw):
                self.build(raw_record=raw)
                self.reject()

    def test_single_matching_dist_info_layout(self):
        for name in ("other-1.0.dist-info/RECORD", "demo/nested.dist-info/data",
                     "DEMO-1.0.dist-info/METADATA", "demo-2.0.dist-info/METADATA"):
            with self.subTest(name=name):
                self.build([(name, b"data")])
                self.reject()

    def test_missing_record_member(self):
        self.build()
        self.wheel.write_bytes(self.wheel.read_bytes().replace(b"/RECORD", b"/NOTREC"))
        self.reject()

    def test_file_implicit_directory_collision(self):
        self.build([("demo", b"file"), ("Demo/data", b"data")])
        self.reject()

    def test_zip64_sentinel_multidisk_and_count_preflight(self):
        for relative, fmt, value in ((10, "H", 0xffff), (4, "H", 1),
                                     (16, "I", 0xffffffff), (10, "H", 1)):
            with self.subTest(relative=relative, value=value):
                self.build()
                data = bytearray(self.wheel.read_bytes())
                end = data.rfind(b"PK\x05\x06")
                struct.pack_into("<" + fmt, data, end + relative, value)
                self.wheel.write_bytes(data)
                with patch.object(zipfile, "ZipFile", side_effect=AssertionError("preflight first")):
                    self.reject()

    def test_truncated_and_nonzip_archive(self):
        self.build()
        raw = self.wheel.read_bytes()
        for data in (b"not a zip", raw[:-12], raw + b"unexpected trailer"):
            with self.subTest(size=len(data)):
                self.wheel.write_bytes(data)
                self.reject()

    def test_local_name_and_method_mismatch(self):
        for method in (False, True):
            with self.subTest(method=method):
                self.build()
                data = bytearray(self.wheel.read_bytes())
                if method:
                    struct.pack_into("<H", data, 8, zipfile.ZIP_DEFLATED)
                else:
                    data[30] = ord("X")
                self.wheel.write_bytes(data)
                self.reject()

    def test_local_crc_size_and_zip64_extra_mismatch(self):
        for offset, value, force_zip64 in ((14, 0, False), (18, 0, False),
                                            (22, 0, False), (18, 0xffffffff, False),
                                            (22, 0, True)):
            with self.subTest(offset=offset, force_zip64=force_zip64):
                self.build(force_zip64=force_zip64)
                data = bytearray(self.wheel.read_bytes())
                struct.pack_into("<I", data, offset, value)
                self.wheel.write_bytes(data)
                self.reject()

    def test_aggregate_decompression_limit(self):
        self.build([("demo/a", b"a" * 80), ("demo/b", b"b" * 80)])
        # Both payloads individually fit; aggregate data+RECORD does not.
        self.reject(max_uncompressed=150)


if __name__ == "__main__":
    unittest.main()
