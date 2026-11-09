"""Offline, read-only wheel payload evidence for a release CANDIDATE.

Caller pins are required but their provenance is NOT authenticated here. No
archive code is imported, loaded, extracted or executed. Native names are only
an inventory, not Authenticode, compatibility or safety evidence. This is
separate from the Broker AST audit; protected Broker/ChatGPT IPC stay HOLD_LIVE.

Profile: ordinary Windows fixed-drive files; bounded single-disk ZIP, stored
or deflated members, UTF8 names/RECORD, sha256 RECORD rows. Central ZIP64 and
unlisted signature sidecars are outside this conservative profile (signature
verification is not implemented). Bounded local force_zip64 headers work.
WHEEL metadata is covered as bytes, not interpreted or used to authorize tags.
Path checks are observations, not a concurrent hostile namespace/ACL proof.
"""
import base64
import csv
import ctypes
from dataclasses import dataclass, field
import hashlib
import hmac
import io
import os
from pathlib import Path
import re
import stat
import struct
import zipfile
import zlib


MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 10_000
MAX_UNCOMPRESSED = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_RECORD_BYTES = 4 * 1024 * 1024
CHUNK = 64 * 1024


class WheelPayloadAuditError(ValueError):
    """No valid payload evidence; never interpret rejection as authorization."""


@dataclass(frozen=True, slots=True)
class WheelPayloadEvidence:
    archive_sha256: str
    archive_size: int
    entry_count: int
    native_module_files: tuple[str, ...]
    record_coverage_verdict: str = field(default="PASS", init=False)
    install_permitted: bool = field(default=False, init=False)
    publisher_authenticated: bool = field(default=False, init=False)
    runtime_authorized: bool = field(default=False, init=False)
    status: str = field(default="HOLD_LIVE", init=False)


def _fixed_drive(anchor):
    # Only OS drive metadata; never load a binary from the candidate archive.
    if os.name != "nt":
        return False
    get_type = ctypes.windll.kernel32.GetDriveTypeW
    get_type.argtypes = (ctypes.c_wchar_p,)
    get_type.restype = ctypes.c_uint
    return get_type(anchor) == 3  # DRIVE_FIXED


def _ordinary_path(path):
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise WheelPayloadAuditError("reparse path denied")
        if part == path:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WheelPayloadAuditError("ordinary single-link file required")
        elif not stat.S_ISDIR(info.st_mode):
            raise WheelPayloadAuditError("ordinary ancestor required")
    return info


def _path(path):
    if not isinstance(path, (str, Path)):
        raise WheelPayloadAuditError("explicit absolute wheel path required")
    raw = str(path)
    if (re.match(r"^[A-Za-z]:[\\/]", raw) is None
            or any(ord(c) < 32 for c in raw) or ":" in raw[2:]
            or any(not p or p in (".", "..") or p.endswith((" ", "."))
                   or any(c in p for c in '<>"|?*')
                   for p in re.split(r"[\\/]", raw[3:]))):
        raise WheelPayloadAuditError("ambiguous/nonlocal wheel path denied")
    result = Path(raw)
    if (not result.is_absolute() or not _fixed_drive(result.anchor)
            or result.suffix != ".whl"):
        raise WheelPayloadAuditError("absolute fixed-drive .whl required")
    bits = result.name[:-4].split("-")
    if (len(bits) not in (5, 6)
            or any(re.fullmatch(r"[A-Za-z0-9_.+]+", b) is None for b in bits)
            or (len(bits) == 6 and not bits[2][0].isdigit())):
        raise WheelPayloadAuditError("invalid wheel filename")
    return result, bits[0] + "-" + bits[1] + ".dist-info"


def _identity(info):
    # Windows 3.13 lstat/fstat can expose different ctime meanings (birth/change
    # time). Use stable file ID, size, mtime and link count across both APIs.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_nlink)


def _snapshot(path, expected_size):
    before = _ordinary_path(path)
    if before.st_size != expected_size:
        raise WheelPayloadAuditError("archive size differs from caller pin")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or _identity(opened) != _identity(before)):
            raise WheelPayloadAuditError("archive changed before read")
        data = stream.read(expected_size + 1)
        after = os.fstat(stream.fileno())
    if (len(data) != expected_size or _identity(after) != _identity(before)
            or _identity(_ordinary_path(path)) != _identity(before)):
        raise WheelPayloadAuditError("archive changed during read")
    return data


def _member_name(name):
    if (not name or name.startswith("/") or "\\" in name or ":" in name
            or any(ord(c) < 32 for c in name)
            or any(p in ("", ".", "..") or p.endswith((" ", "."))
                   or any(c in p for c in '<>"|?*') for p in name.split("/"))):
        raise WheelPayloadAuditError("unsafe ZIP/RECORD member name")
    # Windows device aliases must not become seemingly ordinary payload names.
    for part in name.split("/"):
        stem = part.split(".")[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} or re.fullmatch(
                r"(?:COM|LPT)[1-9¹²³]", stem):
            raise WheelPayloadAuditError("Windows device member name denied")
    return name


def _central_bound(data, maximum):
    """Bound the central directory BEFORE ZipFile allocates its entry list."""
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if end < 0 or end + 22 > len(data):
        raise WheelPayloadAuditError("missing/truncated ZIP end record")
    _, disk, start_disk, count_disk, count, size, offset, comment = struct.unpack_from(
        "<4s4H2IH", data, end)
    if (end + 22 + comment != len(data) or disk or start_disk
            or count_disk != count or not 0 < count <= maximum
            or count == 0xffff or size == 0xffffffff or offset == 0xffffffff
            or offset + size != end):
        raise WheelPayloadAuditError("unbounded/ZIP64/multidisk ZIP directory")
    pos = offset
    for _ in range(count):
        if pos + 46 > end or data[pos:pos + 4] != b"PK\x01\x02":
            raise WheelPayloadAuditError("malformed central directory")
        flags = struct.unpack_from("<H", data, pos + 8)[0]
        compressed, uncompressed = struct.unpack_from("<II", data, pos + 20)
        n, extra, note, volume = struct.unpack_from("<4H", data, pos + 28)
        local = struct.unpack_from("<I", data, pos + 42)[0]
        stop = pos + 46 + n + extra + note
        if (stop > end or volume or local == 0xffffffff
                or compressed == 0xffffffff or uncompressed == 0xffffffff):
            raise WheelPayloadAuditError("invalid/ZIP64 central member")
        raw = data[pos + 46:pos + 46 + n]
        name = raw.decode("utf-8", errors="strict")
        if not flags & 0x800 and not raw.isascii():
            raise WheelPayloadAuditError("ambiguous ZIP name encoding")
        _member_name(name)
        pos = stop
    if pos != end:
        raise WheelPayloadAuditError("central count/size mismatch")
    return count, offset


def _members(archive, data, directory, expected_count, maximum, dist_info):
    infos = archive.infolist()
    if len(infos) != expected_count:
        raise WheelPayloadAuditError("central entry count mismatch")
    seen = set()
    names = set()
    dist_dirs = set()
    total = compressed = 0
    ranges = []
    for info in infos:
        name = _member_name(info.orig_filename)
        key = name.casefold()
        if key in seen or name != info.filename:
            raise WheelPayloadAuditError("duplicate/casefold ZIP member")
        seen.add(key)
        names.add(name)
        parts = name.split("/")
        for index, part in enumerate(parts):
            if part.casefold().endswith(".dist-info"):
                if index != 0 or len(parts) < 2:
                    raise WheelPayloadAuditError("nested/invalid dist-info layout")
                dist_dirs.add(part)
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if (info.is_dir() or kind not in (0, stat.S_IFREG)
                or info.external_attr & 0x10):
            raise WheelPayloadAuditError("nonregular ZIP member")
        if info.flag_bits & (1 | 0x40 | 0x2000):
            raise WheelPayloadAuditError("encrypted ZIP member")
        if (info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or not 0 <= info.file_size <= maximum
                or not 0 <= info.compress_size <= len(data)
                or info.file_size > max(1, info.compress_size) * MAX_COMPRESSION_RATIO):
            raise WheelPayloadAuditError("compression/size/ratio bound exceeded")
        total += info.file_size
        compressed += info.compress_size
        if total > maximum or compressed > len(data):
            raise WheelPayloadAuditError("aggregate ZIP byte bound exceeded")
        local = info.header_offset
        if local < 0 or local + 30 > directory or data[local:local + 4] != b"PK\x03\x04":
            raise WheelPayloadAuditError("invalid local ZIP header")
        flags, method = struct.unpack_from("<HH", data, local + 6)
        n, extra = struct.unpack_from("<HH", data, local + 26)
        begin = local + 30 + n + extra
        end = begin + info.compress_size
        if (flags != info.flag_bits or method != info.compress_type
                or begin > directory or end > directory):
            raise WheelPayloadAuditError("local/central ZIP mismatch")
        crc, local_compressed, local_size = struct.unpack_from("<III", data, local + 14)
        # Local force_zip64 is common even for small standard wheels. Decode
        # its lengths explicitly; never trust unbounded/sentinel extra values.
        pos = local + 30 + n
        zip64 = None
        while pos < begin:
            if pos + 4 > begin:
                raise WheelPayloadAuditError("truncated local ZIP extra")
            tag, size = struct.unpack_from("<HH", data, pos)
            pos += 4
            if pos + size > begin:
                raise WheelPayloadAuditError("truncated local ZIP extra value")
            if tag == 1:
                if zip64 is not None:
                    raise WheelPayloadAuditError("duplicate local ZIP64 extra")
                zip64 = data[pos:pos + size]
            pos += size
        zip64_pos = 0
        sizes = [local_size, local_compressed]
        for index, value in enumerate(sizes):
            if value == 0xffffffff:
                if zip64 is None or zip64_pos + 8 > len(zip64):
                    raise WheelPayloadAuditError("missing local ZIP64 length")
                sizes[index] = struct.unpack_from("<Q", zip64, zip64_pos)[0]
                zip64_pos += 8
        expected = (info.CRC, info.file_size, info.compress_size)
        actual = (crc, *sizes)
        if any(a != e and not (flags & 8 and a == 0)
               for a, e in zip(actual, expected)):
            raise WheelPayloadAuditError("local CRC/size differs from central directory")
        ranges.append((local, end, begin, info))
    if dist_dirs != {dist_info} or dist_info + "/RECORD" not in names:
        raise WheelPayloadAuditError("single matching dist-info/RECORD required")
    # Files and implicit directories cannot alias under Windows case folding.
    for name in names:
        parts = name.casefold().split("/")
        if any("/".join(parts[:i]) in seen for i in range(1, len(parts))):
            raise WheelPayloadAuditError("file/directory member collision")
    previous = 0
    for start, end, _, _info in sorted(ranges, key=lambda row: row[0]):
        if start < previous:
            raise WheelPayloadAuditError("overlapping ZIP members")
        previous = end
    return ranges


def _actual_deflate_length(data, begin, info):
    """ZipExtFile clips at file_size; independently detect hidden extra output."""
    decoder = zlib.decompressobj(-15)
    total = 0
    pos, end = begin, begin + info.compress_size
    pending = b""
    while pos < end or pending:
        if not pending:
            stop = min(pos + CHUNK, end)
            pending = data[pos:stop]
            pos = stop
        output = decoder.decompress(pending, min(CHUNK, info.file_size - total + 1))
        total += len(output)
        pending = decoder.unconsumed_tail
        if total > info.file_size or decoder.unused_data:
            raise WheelPayloadAuditError("actual deflate length exceeds declaration")
    if total != info.file_size or not decoder.eof:
        raise WheelPayloadAuditError("truncated/misdeclared deflate stream")


def _contents(archive, data, ranges, record_name):
    measured = {}
    record = None
    for _, _, begin, info in ranges:
        if info.filename == record_name and info.file_size > MAX_RECORD_BYTES:
            raise WheelPayloadAuditError("RECORD byte bound exceeded")
        if info.compress_type == zipfile.ZIP_DEFLATED:
            _actual_deflate_length(data, begin, info)
        elif info.file_size != info.compress_size:
            raise WheelPayloadAuditError("stored member length mismatch")
        digest = hashlib.sha256()
        count = 0
        crc = 0
        parts = []
        with archive.open(info, "r") as stream:
            while True:
                block = stream.read(min(CHUNK, info.file_size - count + 1))
                if not block:
                    break
                count += len(block)
                if count > info.file_size:
                    raise WheelPayloadAuditError("actual member byte bound exceeded")
                digest.update(block)
                crc = zlib.crc32(block, crc)
                if info.filename == record_name:
                    parts.append(block)
        if count != info.file_size or crc != info.CRC:
            raise WheelPayloadAuditError("actual member length/CRC mismatch")
        measured[info.filename] = (
            "sha256=" + base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii"),
            count)
        if info.filename == record_name:
            record = b"".join(parts)
    return measured, record


def _record_coverage(raw, measured, record_name):
    rows = csv.reader(io.StringIO(raw.decode("utf-8", errors="strict"), newline=""),
                      delimiter=",", strict=True)
    seen = set()
    for row in rows:
        if len(row) != 3 or len(seen) >= len(measured):
            raise WheelPayloadAuditError("invalid RECORD row/count")
        name, digest, size = row
        _member_name(name)
        if name in seen or name not in measured:
            raise WheelPayloadAuditError("duplicate/unlisted RECORD row")
        seen.add(name)
        if name == record_name and digest == size == "":
            continue
        expected_digest, expected_size = measured[name]
        if (re.fullmatch(r"sha256=[A-Za-z0-9_-]{43}", digest) is None
                or not hmac.compare_digest(digest, expected_digest)
                or re.fullmatch(r"[0-9]{1,20}", size) is None
                or int(size) != expected_size):
            raise WheelPayloadAuditError("RECORD hash/size mismatch or missing hash")
    if seen != set(measured):
        raise WheelPayloadAuditError("RECORD coverage incomplete")


def audit_wheel_payload(path, *, expected_sha256, expected_size,
                       max_entries=MAX_ENTRIES, max_uncompressed=MAX_UNCOMPRESSED):
    """Return immutable evidence only on complete bounded hash/CRC/RECORD match.

    Pins must come independently from the caller, never from wheel metadata.
    Their external trust and publisher identity remain unverified. Limits may
    be lowered, never raised beyond the hard profile bounds. No installation,
    native loading, runtime approval or full wheel metadata validation occurs.
    """
    if (type(expected_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or type(expected_size) is not int or not 0 < expected_size <= MAX_ARCHIVE_BYTES
            or type(max_entries) is not int or not 0 < max_entries <= MAX_ENTRIES
            or type(max_uncompressed) is not int
            or not 0 < max_uncompressed <= MAX_UNCOMPRESSED):
        raise WheelPayloadAuditError("strict caller pins and bounded limits required")
    try:
        wheel, dist_info = _path(path)
        data = _snapshot(wheel, expected_size)
        digest = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(digest, expected_sha256):
            raise WheelPayloadAuditError("archive SHA256 differs from caller pin")
        count, directory = _central_bound(data, max_entries)
        record_name = dist_info + "/RECORD"
        with zipfile.ZipFile(io.BytesIO(data), "r", allowZip64=False) as archive:
            ranges = _members(archive, data, directory, count, max_uncompressed, dist_info)
            measured, record = _contents(archive, data, ranges, record_name)
        _record_coverage(record, measured, record_name)
    except (OSError, UnicodeError, csv.Error, struct.error, zipfile.BadZipFile,
            NotImplementedError, RuntimeError, EOFError, zlib.error) as exc:
        raise WheelPayloadAuditError("unreadable or invalid wheel payload") from exc
    native = tuple(sorted(name for name in measured
                          if name.casefold().endswith((".pyd", ".dll"))))
    return WheelPayloadEvidence(digest, len(data), count, native)
