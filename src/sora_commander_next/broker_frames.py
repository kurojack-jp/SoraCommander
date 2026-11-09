"""Bounded length-prefixed IPC frames, independent of transport.

Wire format: unsigned 32-bit little-endian byte count, then exactly that
many bytes. No socket, named-pipe listener, subprocess, or background thread.
"""
import struct
from .broker_protocol import MAX_FRAME


class FrameRejected(ValueError):
    pass


HEADER_SIZE = 4


def pack_frame(payload):
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_FRAME:
        raise FrameRejected("invalid frame payload size")
    return struct.pack("<I", len(payload)) + payload


def read_frame(read_exact):
    """read_exact(n) must return exactly n bytes or raise on EOF/timeout."""
    if not callable(read_exact):
        raise TypeError("read_exact callback required")
    header = read_exact(HEADER_SIZE)
    if type(header) is not bytes or len(header) != HEADER_SIZE:
        raise FrameRejected("missing frame header")
    length = struct.unpack("<I", header)[0]
    if not 0 < length <= MAX_FRAME:
        raise FrameRejected("invalid frame length")
    payload = read_exact(length)
    if type(payload) is not bytes or len(payload) != length:
        raise FrameRejected("truncated frame")
    return payload


def write_frame(write_all, payload):
    if not callable(write_all):
        raise TypeError("write_all callback required")
    data = pack_frame(payload)
    result = write_all(data)
    if result is not None and result != len(data):
        raise FrameRejected("incomplete frame write")
    return len(data)
