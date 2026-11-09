"""DEV_ONLY named pipe between the Host and the Owner Broker. Nothing starts it.

Server side: every request is checked with the existing Owner guard and the
client's impersonation token (SID, logon, session) before dispatch. One pipe
instance stays open for the server's life, so the name is never free to take.

Client side: before sending anything, the client requires that the Owner owns
the pipe object. Both sides run as the same Owner, so this keeps other Windows
users out. It does not stop malicious code running as the Owner.

An I/O buffer stays referenced until Windows reports its completion. If a
cancelled operation does not complete in time, it is retained, not freed.
"""
import ctypes
from ctypes import wintypes
from hashlib import sha256
import os
import re
import struct
import threading
import time

if os.name != "nt":
    raise ImportError("Owner Broker pipe requires Windows")

from .broker_host import MAX_REPLY_BYTES, BrokerUnavailable
from .windows_owner import (ConvertSidToStringSidW, OwnerSessionGuard,
                            current_windows_owner)
from .windows_pipe_connect import OVERLAPPED
from .windows_pipe_context import PipeImpersonationError, WindowsPipeRequestContext

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi = ctypes.WinDLL("advapi32", use_last_error=True)
HANDLE, DWORD, BOOL = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
_INVALID = ctypes.c_void_p(-1).value


def _api(library, name, args, result):
    function = getattr(library, name)
    function.argtypes, function.restype = args, result
    return function


_CreateEvent = _api(_kernel, "CreateEventW",
                    [ctypes.c_void_p, BOOL, BOOL, wintypes.LPCWSTR], HANDLE)
_SetEvent = _api(_kernel, "SetEvent", [HANDLE], BOOL)
_Close = _api(_kernel, "CloseHandle", [HANDLE], BOOL)
_WaitMany = _api(_kernel, "WaitForMultipleObjects",
                 [DWORD, ctypes.POINTER(HANDLE), BOOL, DWORD], DWORD)
_CancelIoEx = _api(_kernel, "CancelIoEx", [HANDLE, ctypes.c_void_p], BOOL)
_GetResult = _api(_kernel, "GetOverlappedResult",
                  [HANDLE, ctypes.c_void_p, ctypes.POINTER(DWORD), BOOL], BOOL)
_ReadFile = _api(_kernel, "ReadFile",
                 [HANDLE, ctypes.c_void_p, DWORD, ctypes.c_void_p, ctypes.c_void_p], BOOL)
_WriteFile = _api(_kernel, "WriteFile",
                  [HANDLE, ctypes.c_void_p, DWORD, ctypes.c_void_p, ctypes.c_void_p], BOOL)
_CreateNamedPipe = _api(_kernel, "CreateNamedPipeW",
                        [wintypes.LPCWSTR, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD,
                         ctypes.c_void_p], HANDLE)
_ConnectNamedPipe = _api(_kernel, "ConnectNamedPipe", [HANDLE, ctypes.c_void_p], BOOL)
_DisconnectNamedPipe = _api(_kernel, "DisconnectNamedPipe", [HANDLE], BOOL)
_CreateFile = _api(_kernel, "CreateFileW",
                   [wintypes.LPCWSTR, DWORD, DWORD, ctypes.c_void_p, DWORD, DWORD, HANDLE],
                   HANDLE)
_WaitNamedPipe = _api(_kernel, "WaitNamedPipeW", [wintypes.LPCWSTR, DWORD], BOOL)
_LocalFree = _api(_kernel, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
_ConvertSddl = _api(_advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                    [wintypes.LPCWSTR, DWORD, ctypes.POINTER(ctypes.c_void_p),
                     ctypes.c_void_p], BOOL)
_GetSecurityInfo = _api(_advapi, "GetSecurityInfo",
                        [HANDLE, ctypes.c_int, DWORD, ctypes.POINTER(ctypes.c_void_p),
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_void_p)], DWORD)


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", BOOL)]


_PIPE_FLAGS = 0x00000003 | 0x40000000  # duplex, overlapped
_FIRST_INSTANCE = 0x00080000
_REJECT_REMOTE = 0x00000008
_ERROR_IO_PENDING, _ERROR_PIPE_CONNECTED = 997, 535
_ERROR_FILE_NOT_FOUND, _ERROR_PIPE_BUSY = 2, 231
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF
_CHUNK = 65536
_HEADER = struct.Struct("<I")
_CANCEL_GRACE_MS = 5000
_READ_REQUEST_SECONDS = 5.0
_WRITE_REPLY_SECONDS = 30.0
_MAX_WORKERS = 8
_NAME_PREFIX = "\\\\.\\pipe\\SoraCommanderNext-"
# Windows gives a same-user client READ plus the write bit it needs. This avoids
# FILE_CREATE_PIPE_INSTANCE, which GENERIC_WRITE would also request.
_CLIENT_ACCESS = 0x80000000 | 0x0002  # GENERIC_READ | FILE_WRITE_DATA
_CLIENT_FLAGS = 0x40000000 | 0x00100000 | (2 << 16)  # overlapped, SQOS, impersonation

# Operations that outlived their cancel grace stay referenced, on purpose.
_retained = []
_retained_lock = threading.Lock()


class BrokerPipeError(OSError):
    pass


def broker_pipe_name(sid, session_id):
    """Fixed per Owner logon session. It carries no secret and proves nothing."""
    if type(sid) is not str or type(session_id) is not int:
        raise ValueError("Owner identity required")
    digest = sha256(sid.encode("ascii")).hexdigest()[:24]
    return f"{_NAME_PREFIX}Broker-{session_id}-{digest}"


def _valid_name(name):
    return (type(name) is str and name.startswith(_NAME_PREFIX)
            and re.fullmatch("[A-Za-z0-9-]{1,96}", name[len(_NAME_PREFIX):]) is not None)


def _io(handle, issue, deadline, stop_event=None, connect=False):
    """One overlapped call. Returns the byte count once Windows confirms it."""
    event = _CreateEvent(None, True, False, None)
    if not event:
        raise BrokerPipeError("pipe event unavailable")
    overlapped = OVERLAPPED()
    overlapped.hEvent = event
    finished = False
    try:
        count = DWORD()
        if issue(ctypes.byref(overlapped)):
            finished = True
            if not connect and not _GetResult(handle, ctypes.byref(overlapped),
                                              ctypes.byref(count), False):
                raise BrokerPipeError("pipe result unavailable")
            return count.value
        error = ctypes.get_last_error()
        if connect and error == _ERROR_PIPE_CONNECTED:
            finished = True
            return 0
        if error != _ERROR_IO_PENDING:
            finished = True
            raise BrokerPipeError("pipe operation refused", error)
        handles = (HANDLE * 2)(event, stop_event or event)
        if deadline is None:
            wait_ms = _INFINITE
        else:
            wait_ms = max(1, min(_INFINITE - 1, int((deadline - time.monotonic()) * 1000)))
        status = _WaitMany(2 if stop_event else 1, handles, False, wait_ms)
        if status != 0:
            _CancelIoEx(handle, ctypes.byref(overlapped))
            one = (HANDLE * 1)(event)
            if _WaitMany(1, one, False, _CANCEL_GRACE_MS) != 0:
                raise BrokerPipeError("cancelled pipe operation still pending")
            finished = True
            _GetResult(handle, ctypes.byref(overlapped), ctypes.byref(count), False)
            raise TimeoutError("pipe operation stopped")
        finished = True
        if not _GetResult(handle, ctypes.byref(overlapped), ctypes.byref(count), False):
            raise BrokerPipeError("pipe completion failed", ctypes.get_last_error())
        return count.value
    finally:
        if finished:
            _Close(event)
        else:
            # The kernel may still write into these. Never free them.
            with _retained_lock:
                _retained.append((overlapped, event))


def _read_exact(handle, size, deadline):
    data = bytearray()
    while len(data) < size:
        want = min(_CHUNK, size - len(data))
        buffer = ctypes.create_string_buffer(want)
        try:
            count = _io(handle, lambda ov: _ReadFile(handle, buffer, want, None, ov), deadline)
        except BrokerPipeError:
            with _retained_lock:
                _retained.append(buffer)
            raise
        if not 0 < count <= want:
            raise BrokerPipeError("pipe closed during read")
        data.extend(buffer.raw[:count])
    return bytes(data)


def _write_all(handle, payload, deadline):
    offset = 0
    while offset < len(payload):
        chunk = payload[offset:offset + _CHUNK]
        buffer = ctypes.create_string_buffer(chunk, len(chunk))
        try:
            count = _io(handle, lambda ov: _WriteFile(handle, buffer, len(chunk), None, ov),
                        deadline)
        except BrokerPipeError:
            with _retained_lock:
                _retained.append(buffer)
            raise
        if not 0 < count <= len(chunk):
            raise BrokerPipeError("pipe closed during write")
        offset += count


def _read_frame(handle, deadline, limit):
    size = _HEADER.unpack(_read_exact(handle, _HEADER.size, deadline))[0]
    if not 0 < size <= limit:
        raise BrokerPipeError("invalid frame length")
    return _read_exact(handle, size, deadline)


def _write_frame(handle, payload, deadline, limit):
    if type(payload) is not bytes or not 0 < len(payload) <= limit:
        raise BrokerPipeError("invalid frame payload")
    _write_all(handle, _HEADER.pack(len(payload)) + payload, deadline)


class OwnerBrokerPipeServer:
    """Fixed number of pipe instances, each with one thread. No restart logic."""

    def __init__(self, *, owner_guard, service, pipe_name, workers=4):
        if type(owner_guard) is not OwnerSessionGuard:
            raise TypeError("trusted Owner guard required")
        if not _valid_name(pipe_name):
            raise ValueError("unapproved pipe name")
        if type(workers) is not int or not 1 <= workers <= _MAX_WORKERS:
            raise ValueError("invalid worker count")
        self._guard, self._name, self._workers = owner_guard, pipe_name, workers
        self._context = WindowsPipeRequestContext(owner_guard=owner_guard, dispatcher=service,
                                                  max_frame=MAX_REPLY_BYTES)
        self._stop = _CreateEvent(None, True, False, None)
        if not self._stop:
            raise BrokerPipeError("stop event unavailable")
        self._handles, self._threads = [], []
        self._started = self._stopped = False

    def _create_instance(self, first, descriptor):
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        flags = _PIPE_FLAGS | (_FIRST_INSTANCE if first else 0)
        handle = _CreateNamedPipe(self._name, flags, _REJECT_REMOTE, self._workers,
                                  _CHUNK, _CHUNK, 0, ctypes.byref(attributes))
        if handle in (None, _INVALID):
            raise BrokerPipeError("secure pipe creation failed", ctypes.get_last_error())
        return handle

    def start(self):
        if self._started:
            raise BrokerPipeError("server is single-use")
        self._started = True
        self._guard.verify()
        descriptor = ctypes.c_void_p()
        # Explicit Owner even with an elevated token; DACL remains unchanged.
        sddl = f"O:{self._guard.sid}D:P(A;;GA;;;{self._guard.sid})(A;;GA;;;SY)"
        if not _ConvertSddl(sddl, 1, ctypes.byref(descriptor), None):
            raise BrokerPipeError("pipe security descriptor unavailable")
        try:
            self._handles = [self._create_instance(index == 0, descriptor)
                             for index in range(self._workers)]
        finally:
            _LocalFree(descriptor)
        for handle in self._handles:
            thread = threading.Thread(target=self._serve, args=(handle,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _serve(self, handle):
        while True:
            try:
                _io(handle, lambda ov: _ConnectNamedPipe(handle, ov), None,
                    stop_event=self._stop, connect=True)
            except (TimeoutError, OSError):
                return
            try:
                self._serve_one(handle)
            except (OSError, ValueError, struct.error):
                pass  # A refused or broken client gets no reply.
            finally:
                _DisconnectNamedPipe(handle)
            if self._stopped:
                return

    def _serve_one(self, handle):
        raw = _read_frame(handle, time.monotonic() + _READ_REQUEST_SECONDS, MAX_REPLY_BYTES)
        try:
            reply = self._context.handle(HANDLE(handle), raw)
        except PermissionError as error:
            raise BrokerPipeError("client refused") from error
        deadline = time.monotonic() + _WRITE_REPLY_SECONDS
        _write_frame(handle, reply, deadline, MAX_REPLY_BYTES)
        # DisconnectNamedPipe discards unread data, so wait until the client has
        # read the reply and closed. Anything it sends after that is ignored.
        try:
            _read_exact(handle, 1, deadline)
        except (TimeoutError, OSError):
            pass

    def stop(self, timeout=10.0):
        """Stop accepting. Requests already in the backend finish on their own."""
        self._stopped = True
        _SetEvent(self._stop)
        for thread in self._threads:
            thread.join(timeout)
        alive = any(thread.is_alive() for thread in self._threads)
        if not alive:
            for handle in self._handles:
                _Close(handle)
            self._handles = []
        return not alive


def _pipe_owner_sid(handle):
    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    # SE_KERNEL_OBJECT = 6, OWNER_SECURITY_INFORMATION = 1
    if _GetSecurityInfo(handle, 6, 1, ctypes.byref(owner), None, None, None,
                        ctypes.byref(descriptor)) != 0:
        raise BrokerPipeError("pipe owner unavailable")
    try:
        text = wintypes.LPWSTR()
        if not ConvertSidToStringSidW(owner, ctypes.byref(text)):
            raise BrokerPipeError("pipe owner unreadable")
        try:
            return text.value
        finally:
            _LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        _LocalFree(descriptor)


class OwnerBrokerPipeClient:
    """BrokerHost transport. One connection per request and no automatic retry.

    BrokerUnavailable means the request was known not to be sent. Any failure
    after sending begins reaches BrokerHost as an unconfirmed outcome.
    """

    def __init__(self, *, pipe_name, reply_seconds=150.0, busy_wait_ms=1000):
        if not _valid_name(pipe_name):
            raise ValueError("unapproved pipe name")
        if type(reply_seconds) not in (int, float) or not 0 < reply_seconds <= 600:
            raise ValueError("invalid reply deadline")
        self._name, self._reply_seconds, self._busy_wait_ms = (pipe_name, reply_seconds,
                                                               busy_wait_ms)

    def _open(self):
        for attempt in (1, 2):
            handle = _CreateFile(self._name, _CLIENT_ACCESS, 0, None, 3, _CLIENT_FLAGS, None)
            if handle not in (None, _INVALID):
                return handle
            error = ctypes.get_last_error()
            if error == _ERROR_PIPE_BUSY and attempt == 1:
                _WaitNamedPipe(self._name, self._busy_wait_ms)
                continue
            raise BrokerUnavailable("Broker pipe unavailable")
        raise BrokerUnavailable("Broker pipe busy")

    def request(self, raw):
        handle = self._open()
        try:
            try:
                owner = _pipe_owner_sid(handle)
                expected = current_windows_owner().sid
            except Exception:
                raise BrokerUnavailable("Broker pipe owner unconfirmed") from None
            if owner != expected:
                raise BrokerUnavailable("Broker pipe owned by another identity")
            _write_frame(handle, raw, time.monotonic() + _READ_REQUEST_SECONDS, MAX_REPLY_BYTES)
            return _read_frame(handle, time.monotonic() + self._reply_seconds, MAX_REPLY_BYTES)
        finally:
            _Close(handle)

    def close(self):
        pass
