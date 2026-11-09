"""Validated offline PNG branding for MCP server/tools.

Does not supply artwork or fetch images. A release owner must explicitly
pass a locally approved PNG whose SHA-256 is pinned by trusted release
metadata. No untrusted URI, file: URL, HTTP fetch or arbitrary path.
"""
import base64
import hashlib
from pathlib import Path
import struct
import zlib

MAX_ICON_BYTES = 128 * 1024
MIN_DIMENSION = 16
MAX_DIMENSION = 512
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PluginIconError(ValueError):
    pass


def inspect_icon_png(file_bytes):
    """Bounded static PNG validation, including CRC of all chunks."""
    if type(file_bytes) is not bytes or not 48 <= len(file_bytes) <= MAX_ICON_BYTES:
        raise PluginIconError("icon must be a bounded PNG")
    if not file_bytes.startswith(PNG_SIGNATURE):
        raise PluginIconError("icon is not PNG")
    pos = len(PNG_SIGNATURE)
    chunks = []
    dimensions = None
    idat = False
    while pos < len(file_bytes):
        if len(file_bytes) - pos < 12:
            raise PluginIconError("truncated PNG")
        count = struct.unpack_from(">I", file_bytes, pos)[0]
        kind = file_bytes[pos+4:pos+8]
        end = pos + 12 + count
        if count > MAX_ICON_BYTES or end > len(file_bytes):
            raise PluginIconError("truncated PNG chunk")
        payload = file_bytes[pos+8:pos+8+count]
        crc = struct.unpack_from(">I", file_bytes, pos+8+count)[0]
        if (zlib.crc32(kind + payload) & 0xFFFFFFFF) != crc:
            raise PluginIconError("bad PNG chunk checksum")
        if kind == b"IHDR":
            if chunks or count != 13:
                raise PluginIconError("invalid PNG header")
            w, h, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload)
            if (not MIN_DIMENSION <= w <= MAX_DIMENSION or
                    not MIN_DIMENSION <= h <= MAX_DIMENSION or
                    depth != 8 or color not in (2, 6) or
                    compression != 0 or filtering != 0 or interlace != 0):
                raise PluginIconError("unsupported icon size or PNG format")
            dimensions = (w, h)
        elif kind == b"IDAT":
            idat = True
        elif kind == b"IEND":
            if count or not idat or end != len(file_bytes):
                raise PluginIconError("invalid PNG ending")
        elif kind not in (b"sRGB", b"pHYs", b"gAMA", b"cHRM", b"PLTE", b"tRNS"):
            # No text, executable-ish metadata, private chunks, animations.
            raise PluginIconError("unapproved PNG chunk type")
        chunks.append(kind)
        pos = end
    if (dimensions is None or chunks[-1] != b"IEND"
            or chunks.count(b"IHDR") != 1
            or chunks.count(b"IEND") != 1):
        raise PluginIconError("incomplete PNG")
    return dimensions


def pinned_plugin_icon(path, *, expected_sha256):
    """Make an MCP data-URI Icon only from explicitly pinned local PNG bytes."""
    if (type(expected_sha256) is not str or len(expected_sha256) != 64 or
            any(c not in "0123456789abcdef" for c in expected_sha256)):
        raise PluginIconError("trusted image digest required")
    p = Path(path)
    if not p.is_absolute() or p.is_symlink() or (
            hasattr(p, "is_junction") and p.is_junction()) or not p.is_file():
        raise PluginIconError("only a regular absolute PNG file can be used")
    if p.stat().st_size > MAX_ICON_BYTES:
        raise PluginIconError("oversized PNG")
    data = p.read_bytes()
    w, h = inspect_icon_png(data)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise PluginIconError("plugin icon differs from trusted release digest")
    return {
        "src": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
        "mime_type": "image/png",
        "sizes": [f"{w}x{h}"],
    }
