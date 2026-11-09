"""Bounded Windows named-pipe connection acceptance (overlapped I/O)."""
import ctypes
from ctypes import wintypes
import math
import time
from .windows_pipe_lifetime import REGISTRY

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
DWORD = wintypes.DWORD
HANDLE = wintypes.HANDLE
ULONG_PTR = ctypes.c_size_t

class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ULONG_PTR), ("InternalHigh", ULONG_PTR),
                ("Offset", DWORD), ("OffsetHigh", DWORD), ("hEvent", HANDLE)]

_create_event = _kernel.CreateEventW
_create_event.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_create_event.restype = HANDLE
_connect = _kernel.ConnectNamedPipe
_connect.argtypes = [HANDLE, ctypes.POINTER(OVERLAPPED)]
_connect.restype = wintypes.BOOL
_wait = _kernel.WaitForSingleObject
_wait.argtypes = [HANDLE, DWORD]
_wait.restype = DWORD
_cancel = _kernel.CancelIoEx
_cancel.argtypes = [HANDLE, ctypes.POINTER(OVERLAPPED)]
_cancel.restype = wintypes.BOOL
_result = _kernel.GetOverlappedResult
_result.argtypes = [HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(DWORD), wintypes.BOOL]
_result.restype = wintypes.BOOL
_close = _kernel.CloseHandle
_close.argtypes = [HANDLE]
_close.restype = wintypes.BOOL

ERROR_IO_PENDING = 997
ERROR_PIPE_CONNECTED = 535
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258


class PipeConnectError(OSError):
    pass


def _run_operation(handle, *, size, make_buffer, issue, is_connect, deadline,
                   clock, registry, api, error_type):
    """Use a registry-owned pipe. Keep every uncertain native reference alive."""
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("finite pipe deadline required")
    if deadline <= clock():
        raise TimeoutError("pipe deadline exceeded")
    create_event, wait, cancel, result, close, last_error = api
    operation = registry.begin(handle, size)
    try:
        operation.buffer = make_buffer() if size else None
        operation.overlapped = OVERLAPPED()
        operation.close_event = close
        event = create_event(None, True, False, None)
        if not event:
            raise error_type("pipe event unavailable")
        operation.event = event
        operation.overlapped.hEvent = event
        pointer = ctypes.byref(operation.overlapped)

        def probe():
            count = DWORD()
            success = bool(result(handle, pointer, ctypes.byref(count), False))
            error = 0 if success else last_error()
            # Other errors can be query misuse rather than I/O completion.
            operation.terminal = success or error in (995, 109, 232)
            return success, count.value, error

        operation.probe = probe
        # An exception in the native issuance call is not proof of nonissuance.
        operation.terminal = False
        immediate = bool(issue(handle, operation.buffer, pointer))
        error = 0 if immediate else last_error()
        if is_connect and (immediate or error == ERROR_PIPE_CONNECTED):
            operation.terminal = True
            count = 0  # Connect's transferred byte count is undefined.
        else:
            if not immediate and error != ERROR_IO_PENDING:
                operation.terminal = True  # Native call explicitly rejected issuance.
                raise error_type("pipe operation refused", error)
            if not immediate:
                remaining = deadline - clock()
                status = (wait(event, max(1, min(60000, int(remaining * 1000))))
                          if remaining > 0 else WAIT_TIMEOUT)
                if status != WAIT_OBJECT_0:
                    operation.expired = operation.cancel_requested = True
                    cancel(handle, pointer)  # Exactly one request; not completion evidence.
                    probe()  # Never wait(TRUE), even after successful cancellation.
                    if status == WAIT_TIMEOUT:
                        raise TimeoutError("pipe deadline exceeded")
                    raise error_type("pipe wait failed")
            success, count, error = probe()
            if not success:
                raise error_type("pipe completion failed or unconfirmed", error)
        if clock() >= deadline:
            operation.expired = True
            raise TimeoutError("pipe result arrived after deadline")
        if not is_connect and not 0 <= count <= size:
            raise error_type("invalid native byte count")
        return operation.buffer, count
    finally:
        registry.finish(operation)


def connect_until(handle, *, timeout_ms=5000):
    """Accept on a registry-owned pipe, retaining unconfirmed I/O on failure."""
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 60000:
        raise ValueError("invalid connect timeout")
    _run_operation(handle, size=0, make_buffer=None,
                   issue=lambda h, buf, op: _connect(h, op), is_connect=True,
                   deadline=time.monotonic() + timeout_ms / 1000, clock=time.monotonic,
                   registry=REGISTRY,
                   api=(_create_event, _wait, _cancel, _result, _close, ctypes.get_last_error),
                   error_type=PipeConnectError)
    return True
