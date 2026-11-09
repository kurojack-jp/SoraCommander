"""DEV_ONLY bounded stdout/stderr spool: data, not job execution or consent.

This module is an isolated building block for D08. It MUST NOT be used as a
production output repository until protected Broker ownership, single-writer
exclusion, Owner ACL, reconnect authentication and retention are accepted.
No MCP endpoint, child spawn, command replay or service is registered here.
"""
import base64
from hashlib import sha256
import os
from pathlib import Path
import stat
import struct

from .file_reads import _check, ReadBoundaryError

MAGIC = b"SCNSPL01"
HEADER = struct.Struct(">BI32s")
STREAMS = {"stdout": 1, "stderr": 2}
REVERSE_STREAMS = {v: k for k, v in STREAMS.items()}
SPOOL_NAME = "owner-output-dev.spool"
MAX_CAPTURE = 16 * 1024 * 1024
MAX_RECORD = 4096
MAX_RECORDS = 8192
MAX_PAGE = 4096


class OutputSpoolError(ValueError):
    """Corruption/unsupported storage must never masquerade as completed output."""


class OutputSpoolFull(OutputSpoolError):
    """Caller must keep draining the OS pipe even if persistent capture is full."""


def _target(path, roots, *, existing):
    if type(path) is not str:
        raise OutputSpoolError("invalid spool path")
    p = Path(path)
    if not p.is_absolute() or p.name != SPOOL_NAME:
        raise OutputSpoolError("dev-only spool filename required")
    try:
        parent = _check(str(p.parent), roots)
    except (ReadBoundaryError, OSError, ValueError) as exc:
        raise OutputSpoolError("spool directory outside local roots") from exc
    if parent != p.parent:
        raise OutputSpoolError("spool parent changed")
    if existing:
        try:
            st = p.lstat()
        except OSError as exc:
            raise OutputSpoolError("spool unavailable") from exc
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
                or (hasattr(p, "is_junction") and p.is_junction())
                or p.is_symlink()):
            raise OutputSpoolError("spool must be ordinary single-link file")
    elif p.exists() or p.is_symlink():
        raise OutputSpoolError("spool already exists")
    return p


def _check_open_path(path, roots, opened):
    p = _target(path, roots, existing=True)
    named = p.lstat()
    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)):
        raise OutputSpoolError("spool identity changed")


def _scan(stream):
    stream.seek(0)
    if stream.read(len(MAGIC)) != MAGIC:
        raise OutputSpoolError("invalid spool header")
    records = []
    total = 0
    while True:
        header = stream.read(HEADER.size)
        if not header:
            break
        if len(header) != HEADER.size:
            raise OutputSpoolError("incomplete output record header")
        tag, length, expected = HEADER.unpack(header)
        if tag not in REVERSE_STREAMS or not 1 <= length <= MAX_RECORD:
            raise OutputSpoolError("invalid output record")
        if len(records) >= MAX_RECORDS or total + length > MAX_CAPTURE:
            raise OutputSpoolError("output storage limit exceeded")
        data = stream.read(length)
        if len(data) != length or sha256(data).digest() != expected:
            raise OutputSpoolError("incomplete or corrupted output record")
        records.append((total, REVERSE_STREAMS[tag], data))
        total += length
    return total, records


def _scan_checked(stream, path, roots):
    before = os.fstat(stream.fileno())
    _check_open_path(path, roots, before)
    size, records = _scan(stream)
    after = os.fstat(stream.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise OutputSpoolError("spool changed during scan")
    _check_open_path(path, roots, after)
    return size, records


def create_dev_spool(path, roots):
    """Create one empty disposable capture using exclusive creation."""
    p = _target(path, roots, existing=False)
    try:
        with p.open("x+b") as stream:
            stream.write(MAGIC)
            stream.flush()
            os.fsync(stream.fileno())
            _check_open_path(path, roots, os.fstat(stream.fileno()))
    except FileExistsError as exc:
        raise OutputSpoolError("spool already exists") from exc
    return {"status": "CREATED_DEV_ONLY", "captured_bytes": 0}


def append_dev_output(path, roots, stream_name, data):
    """Append raw bytes, after validating the full bounded prior capture.

    Single-writer API ONLY; no cross-process mutex or authority supplied.
    Never kill jobs when OutputSpoolFull is raised: the caller must drain.
    """
    if (type(stream_name) is not str or stream_name not in STREAMS
            or type(data) is not bytes or not 1 <= len(data) <= MAX_RECORD):
        raise OutputSpoolError("invalid output append")
    p = _target(path, roots, existing=True)
    with p.open("r+b", buffering=0) as stream:
        total, records = _scan_checked(stream, path, roots)
        if total + len(data) > MAX_CAPTURE or len(records) >= MAX_RECORDS:
            raise OutputSpoolFull("capture full; keep draining the child")
        packet = HEADER.pack(STREAMS[stream_name], len(data),
                             sha256(data).digest()) + data
        stream.seek(0, os.SEEK_END)
        if stream.write(packet) != len(packet):
            raise OutputSpoolError("partial spool append; manual reconciliation")
        stream.flush()
        os.fsync(stream.fileno())
        _check_open_path(path, roots, os.fstat(stream.fileno()))
    return {"captured_bytes": total + len(data), "records": len(records) + 1}


def read_dev_output_page(path, roots, *, offset=0, max_bytes=4096):
    """Page interleaved stdout/stderr with a logical, not physical, cursor."""
    if (type(offset) is not int or not 0 <= offset <= MAX_CAPTURE
            or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_PAGE):
        raise OutputSpoolError("invalid output cursor")
    p = _target(path, roots, existing=True)
    with p.open("rb") as stream:
        total, records = _scan_checked(stream, path, roots)
    if offset > total:
        raise OutputSpoolError("cursor beyond archived data")
    sections = []
    remaining = max_bytes
    for at, stream_name, data in records:
        end = at + len(data)
        if end <= offset or remaining == 0:
            continue
        start_in = max(offset - at, 0)
        piece = data[start_in:start_in + remaining]
        sections.append({
            "offset": at + start_in, "stream": stream_name,
            "data_base64": base64.b64encode(piece).decode("ascii"),
        })
        remaining -= len(piece)
    consumed = max_bytes - remaining
    next_offset = offset + consumed
    return {
        "offset": offset, "next_offset": next_offset, "captured_bytes": total,
        "sections": sections, "truncated": next_offset < total,
        "status": "DATA_ONLY_UNVERIFIED",
    }
