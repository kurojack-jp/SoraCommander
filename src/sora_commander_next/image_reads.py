"""Bounded PNG/JPEG reads through the existing read-only boundary."""
import os
import re
import stat
import struct
from hashlib import sha256

from . import file_reads

MAX_IMAGE_BYTES = 4 * 1024 * 1024


class ImageReadError(ValueError):
    pass


def dimensions(raw, fmt):
    if fmt == "PNG":
        if len(raw) < 33 or raw[8:16] != b"\0\0\0\rIHDR":
            raise ImageReadError("NOT_IMAGE")
        width, height = struct.unpack(">II", raw[16:24])
        if not width or not height:
            raise ImageReadError("NOT_IMAGE")
        return width, height
    pos = 2
    while pos < len(raw):
        if raw[pos] != 255:
            break
        while pos < len(raw) and raw[pos] == 255:
            pos += 1
        if pos >= len(raw):
            break
        marker = raw[pos]
        pos += 1
        if marker in (0xD9, 0xDA):
            break
        if marker == 1 or 0xD0 <= marker <= 0xD8:
            continue
        if pos + 2 > len(raw):
            break
        length = int.from_bytes(raw[pos:pos + 2], "big")
        if length < 2 or pos + length > len(raw):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 8:
            height, width = struct.unpack(">HH", raw[pos + 3:pos + 7])
            return (width or None), (height or None)
        pos += length
    return None, None


def read_image(path, roots):
    try:
        if type(path) is not str or not path or "\0" in path:
            raise ImageReadError("PATH_NOT_ALLOWED")
        if re.search(r"~[0-9]", path):
            raise ImageReadError("PATH_NOT_ALLOWED")
        p = file_reads._check(path, roots)
        meta = p.stat()
        if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1
                or getattr(meta, "st_file_attributes", 0) & (0x2 | 0x4 | 0x400)):
            raise ImageReadError("PATH_NOT_ALLOWED")
        if meta.st_size > MAX_IMAGE_BYTES:
            raise ImageReadError("IMAGE_TOO_LARGE")
        with p.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_size > MAX_IMAGE_BYTES
                    or (before.st_dev, before.st_ino) != (meta.st_dev, meta.st_ino)):
                raise ImageReadError("FILE_CHANGED")
            raw = stream.read(MAX_IMAGE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > MAX_IMAGE_BYTES:
            raise ImageReadError("IMAGE_TOO_LARGE")
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if (after.st_nlink != 1 or len(raw) != after.st_size
                or any(getattr(before, f) != getattr(after, f) for f in fields)):
            raise ImageReadError("FILE_CHANGED")
        file_reads._verify_named_file(path, roots, after)
    except (file_reads.ReadBoundaryError, OSError, TypeError, RuntimeError) as exc:
        raise ImageReadError("PATH_NOT_ALLOWED") from exc
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        fmt, mime = "PNG", "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        fmt, mime = "JPEG", "image/jpeg"
    else:
        raise ImageReadError("NOT_IMAGE")
    width, height = dimensions(raw, fmt)
    return ({"path": path, "bytes": len(raw), "sha256": sha256(raw).hexdigest(),
             "format": fmt, "width": width, "height": height,
             "view": "Call image(item) on the image element of content_items to see it."},
            raw, mime)
