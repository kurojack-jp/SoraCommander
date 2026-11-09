"""DEV-only Windows process provenance *observation*, never authentication.

Queries actual Windows process handle, image path, creation FILETIME and
liveness. Reuses the existing OwnerSessionGuard for SID/logon LUID/WTS.
Expected executable is a LOCAL test reference, NOT a trusted code pin.
This cannot verify Authenticode, module hashes, protected ACL, or Broker role.
No process launch, OS configuration, IPC, Owner data or grants.
"""
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import os
from pathlib import Path

if os.name != "nt":
    raise ImportError("Windows process evidence requires Windows")

from .windows_owner import OwnerSessionGuard

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_HANDLE = wintypes.HANDLE
_DWORD = wintypes.DWORD
_BOOL = wintypes.BOOL
_QUERY = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102

_open = k32.OpenProcess
_open.argtypes = (_DWORD, _BOOL, _DWORD)
_open.restype = _HANDLE
_query_path = k32.QueryFullProcessImageNameW
_query_path.argtypes = (_HANDLE, _DWORD, wintypes.LPWSTR, ctypes.POINTER(_DWORD))
_query_path.restype = _BOOL
_times = k32.GetProcessTimes
_times.argtypes = (_HANDLE, ctypes.POINTER(wintypes.FILETIME),
                   ctypes.POINTER(wintypes.FILETIME),
                   ctypes.POINTER(wintypes.FILETIME),
                   ctypes.POINTER(wintypes.FILETIME))
_times.restype = _BOOL
_wait = k32.WaitForSingleObject
_wait.argtypes = (_HANDLE, _DWORD)
_wait.restype = _DWORD
_close = k32.CloseHandle
_close.argtypes = (_HANDLE,)
_close.restype = _BOOL


class DevProcessEvidenceRejected(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class DevProcessEvidence:
    pid: int
    image_path: str
    created_filetime: int
    owner_token_matches: bool
    still_running: bool
    trusted_publisher_verified: bool = False
    protected_install_verified: bool = False
    broker_authorized: bool = False


def observe_owner_process(pid, *, expected_executable, owner_guard):
    """Inspect a *live* Owner process, or fail closed; no grant is returned.

    A held OS process handle prevents PID reuse during this observation.
    The expected executable must be pinned outside of this function by a
    separately trusted install authority before production use.
    """
    if type(pid) is not int or not 1 <= pid <= 0xFFFFFFFF:
        raise DevProcessEvidenceRejected("invalid process identifier")
    if type(owner_guard) is not OwnerSessionGuard:
        raise TypeError("actual OwnerSessionGuard required")
    if (type(expected_executable) is not str
            or not Path(expected_executable).is_absolute()):
        raise DevProcessEvidenceRejected("absolute expected executable required")
    owner_guard.verify()
    handle = _open(_QUERY | _SYNCHRONIZE, False, pid)
    if not handle:
        raise DevProcessEvidenceRejected("process unavailable")
    try:
        if _wait(handle, 0) != _WAIT_TIMEOUT:
            raise DevProcessEvidenceRejected("process exited or liveness unknown")
        buf = ctypes.create_unicode_buffer(32768)
        size = _DWORD(len(buf))
        if not _query_path(handle, 0, buf, ctypes.byref(size)):
            raise DevProcessEvidenceRejected("process image unavailable")
        image = buf.value
        if (size.value == 0 or size.value >= len(buf)
                or os.path.normcase(os.path.normpath(image)) !=
                    os.path.normcase(os.path.normpath(expected_executable))):
            raise DevProcessEvidenceRejected("process image mismatch")
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not _times(handle, ctypes.byref(created), ctypes.byref(exited),
                      ctypes.byref(kernel), ctypes.byref(user)):
            raise DevProcessEvidenceRejected("process creation time unavailable")
        stamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
        if stamp == 0:
            raise DevProcessEvidenceRejected("process creation evidence missing")
        # Same PID remains allocated while this handle is held.
        owner_guard.verify_process(pid)
        owner_guard.verify()
        if _wait(handle, 0) != _WAIT_TIMEOUT:
            raise DevProcessEvidenceRejected("process exited during inspection")
        return DevProcessEvidence(
            pid=pid, image_path=image, created_filetime=stamp,
            owner_token_matches=True, still_running=True)
    finally:
        _close(handle)
