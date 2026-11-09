"""Bounded Windows OVERLAPPED pipe transfers; one in-flight operation."""
import ctypes
from ctypes import wintypes
import time
from .windows_pipe_connect import (
    OVERLAPPED, _create_event, _wait, _cancel, _result, _close,
    ERROR_IO_PENDING, WAIT_OBJECT_0, WAIT_TIMEOUT,
    _run_operation,
)
from .windows_pipe_lifetime import REGISTRY

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
DWORD = wintypes.DWORD
_read = _kernel.ReadFile
_read.argtypes = [wintypes.HANDLE, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD),
                  ctypes.POINTER(OVERLAPPED)]
_read.restype = wintypes.BOOL
_write = _kernel.WriteFile
_write.argtypes = [wintypes.HANDLE, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD),
                   ctypes.POINTER(OVERLAPPED)]
_write.restype = wintypes.BOOL


class PipeTransferError(OSError):
    pass


def _transfer(handle, make_buffer, size, deadline, operation, clock):
    return _run_operation(
        handle, size=size, make_buffer=make_buffer,
        issue=lambda h, buf, op: operation(h, buf, size, None, op),
        is_connect=False, deadline=deadline, clock=clock, registry=REGISTRY,
        api=(_create_event, _wait, _cancel, _result, _close, ctypes.get_last_error),
        error_type=PipeTransferError)


def read_exact_until(handle, size, deadline, *, clock=time.monotonic):
    if type(size) is not int or not 1 <= size <= 8192:
        raise ValueError("invalid read size")
    data = bytearray()
    while len(data) < size:
        remaining = size - len(data)
        buf, n = _transfer(handle, lambda: ctypes.create_string_buffer(remaining),
                           remaining, deadline, _read, clock)
        if not 0 < n <= size - len(data):
            raise PipeTransferError("pipe disconnected during read")
        data.extend(buf.raw[:n])
    return bytes(data)


def write_all_until(handle, payload, deadline, *, clock=time.monotonic):
    if type(payload) is not bytes or not 0 < len(payload) <= 8196:
        raise ValueError("invalid write payload")
    offset = 0
    while offset < len(payload):
        remaining = payload[offset:]
        buf, n = _transfer(handle, lambda: ctypes.create_string_buffer(remaining, len(remaining)),
                           len(remaining), deadline, _write, clock)
        if not 0 < n <= len(remaining):
            raise PipeTransferError("pipe disconnected during write")
        offset += n
    return offset
