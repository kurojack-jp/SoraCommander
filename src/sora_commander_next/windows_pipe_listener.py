"""Opt-in, single-request Windows local named-pipe listener.

No autostart, thread, service, network socket or process launch. A trusted
Owner shell must explicitly call serve_once. One request per connection.
This is not a deployed independent broker.
"""
import ctypes
from ctypes import wintypes
import os
import time
from .windows_pipe_transfer import read_exact_until, write_all_until
from .windows_pipe_connect import connect_until
from .windows_pipe_lifetime import REGISTRY

if os.name != "nt":
    raise ImportError("Windows named-pipe listener requires Windows")

from .broker_frames import read_frame, pack_frame
from .windows_owner import OwnerSessionGuard
from .windows_pipe_context import WindowsPipeRequestContext

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
adv = ctypes.WinDLL("advapi32", use_last_error=True)
HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
BOOL = wintypes.BOOL
INVALID_HANDLE = ctypes.c_void_p(-1).value
PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
FILE_FLAG_OVERLAPPED = 0x40000000
PIPE_TYPE_BYTE = 0
PIPE_READMODE_BYTE = 0
PIPE_WAIT = 0
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008

class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", BOOL)]

CreateNamedPipeW = k32.CreateNamedPipeW
CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, DWORD, DWORD, DWORD, DWORD, DWORD,
                             DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES)]
CreateNamedPipeW.restype = HANDLE
ConnectNamedPipe = k32.ConnectNamedPipe
ConnectNamedPipe.argtypes = [HANDLE, ctypes.c_void_p]
ConnectNamedPipe.restype = BOOL
ReadFile = k32.ReadFile
ReadFile.argtypes = [HANDLE, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD), ctypes.c_void_p]
ReadFile.restype = BOOL
WriteFile = k32.WriteFile
WriteFile.argtypes = [HANDLE, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD), ctypes.c_void_p]
WriteFile.restype = BOOL
DisconnectNamedPipe = k32.DisconnectNamedPipe
DisconnectNamedPipe.argtypes = [HANDLE]
DisconnectNamedPipe.restype = BOOL
CloseHandle = k32.CloseHandle
CloseHandle.argtypes = [HANDLE]
CloseHandle.restype = BOOL
ConvertSDDL = adv.ConvertStringSecurityDescriptorToSecurityDescriptorW
ConvertSDDL.argtypes = [wintypes.LPCWSTR, DWORD, ctypes.POINTER(ctypes.c_void_p),
                        ctypes.POINTER(DWORD)]
ConvertSDDL.restype = BOOL
LocalFree = k32.LocalFree
LocalFree.argtypes = [ctypes.c_void_p]
LocalFree.restype = ctypes.c_void_p


class PipeListenerError(OSError):
    pass


def _read_exact(handle, length):
    parts = bytearray()
    while len(parts) < length:
        remaining = length - len(parts)
        buffer = ctypes.create_string_buffer(remaining)
        count = DWORD()
        if not ReadFile(handle, buffer, remaining, ctypes.byref(count), None) or count.value == 0:
            raise PipeListenerError("pipe read failed or disconnected")
        parts.extend(buffer.raw[:count.value])
    return bytes(parts)


def _write_all(handle, payload):
    offset = 0
    while offset < len(payload):
        chunk = payload[offset:]
        count = DWORD()
        if not WriteFile(handle, chunk, len(chunk), ctypes.byref(count), None) or count.value == 0:
            raise PipeListenerError("pipe write failed or disconnected")
        offset += count.value
    return offset


def serve_once(*, owner_guard, dispatcher, pipe_name):
    """Serve one client in calling Owner thread; blocks until a client connects.

    The endpoint name must be a fixed, reviewed local name. Caller is
    responsible for lifecycle, availability and preventing concurrent servers.
    Do not invoke from an MCP stdio host expecting disconnect survival.
    """
    if not isinstance(owner_guard, OwnerSessionGuard):
        raise TypeError("trusted Owner guard required")
    if type(pipe_name) is not str or not pipe_name.startswith(r"\\.\pipe\SoraCommanderNext-"):
        raise ValueError("unapproved local pipe name")
    suffix = pipe_name[len(r"\\.\pipe\SoraCommanderNext-"):]
    if not suffix or len(suffix) > 64 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for c in suffix):
        raise ValueError("invalid pipe suffix")
    owner_guard.verify()
    sid = owner_guard.sid
    # Explicit protected DACL: only this Owner SID and SYSTEM; no Everyone.
    sddl = "D:P(A;;GA;;;" + sid + ")(A;;GA;;;SY)"
    descriptor = ctypes.c_void_p()
    if not ConvertSDDL(sddl, 1, ctypes.byref(descriptor), None):
        raise PipeListenerError("pipe security descriptor unavailable")
    connection = None
    try:
        # Admission precedes native pipe creation. Retained pending operations
        # keep this lease rooted after serve_once unwinds.
        connection = REGISTRY.reserve_connection()
        attrs = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
        handle = CreateNamedPipeW(pipe_name, PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
                                  PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT |
                                  PIPE_REJECT_REMOTE_CLIENTS, 1, 8192 + 4, 8192 + 4,
                                  0, ctypes.byref(attrs))
        if handle in (None, INVALID_HANDLE):
            raise PipeListenerError("secure pipe creation failed")
        connection.bind(handle, disconnect=DisconnectNamedPipe, close=CloseHandle)
        try:
            connect_until(handle, timeout_ms=5000)
            deadline = time.monotonic() + 5.0
            raw = read_frame(lambda n: read_exact_until(handle, n, deadline))
            response = WindowsPipeRequestContext(owner_guard=owner_guard,
                                                 dispatcher=dispatcher).handle(HANDLE(handle), raw)
            write_all_until(handle, pack_frame(response), time.monotonic() + 5.0)
        finally:
            connection.close()
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            LocalFree(descriptor)
