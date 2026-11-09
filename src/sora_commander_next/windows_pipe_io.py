"""Bounded named-pipe read using PeekNamedPipe; connection accept is separate."""
import ctypes
from ctypes import wintypes
import time
from .broker_deadline import DeadlineExceeded

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_peek = _kernel.PeekNamedPipe
_peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                  ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_peek.restype = wintypes.BOOL
_read = _kernel.ReadFile
_read.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_read.restype = wintypes.BOOL


def read_exact_until(handle, length, deadline, *, clock=time.monotonic, pause=time.sleep):
    """Read exactly length bytes by a shared absolute deadline.

    Single reader only. Blocking ReadFile requests never exceed bytes already
    reported available by PeekNamedPipe. Does not bound ConnectNamedPipe or
    WriteFile; those require separate lifecycle hardening.
    """
    if type(length) is not int or not 1 <= length <= 8192:
        raise ValueError("invalid read length")
    data = bytearray()
    while len(data) < length:
        remaining = deadline - clock()
        if remaining <= 0:
            raise DeadlineExceeded("pipe read deadline exceeded")
        available = wintypes.DWORD()
        if not _peek(handle, None, 0, None, ctypes.byref(available), None):
            raise OSError("pipe disconnected or peek failed")
        if available.value == 0:
            pause(min(0.02, remaining))
            continue
        size = min(length - len(data), available.value)
        buf = ctypes.create_string_buffer(size)
        count = wintypes.DWORD()
        if not _read(handle, buf, size, ctypes.byref(count), None) or not 0 < count.value <= size:
            raise OSError("pipe disconnected or read failed")
        data.extend(buf.raw[:count.value])
    return bytes(data)
