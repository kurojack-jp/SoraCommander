"""Development-only Windows process lease experiment; no Broker authority.

This module does NOT authenticate an Owner, protect the mutex DACL, preserve
jobs after a crash, or authorize any operation. The OS mutex only demonstrates
cross-process mutual exclusion for the same *test* name within a logon session.
Never import from the production Host/MCP/Broker or publish in release bundle.
"""
import ctypes
from ctypes import wintypes
import os
import re

_DEV_NAME = re.compile(r"Local\\SoraCommanderNext\.DevLease\.[A-Za-z0-9_-]{1,64}\Z")
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102


class DevLeaseError(RuntimeError):
    """The development-only mutex lease could not be obtained safely."""


class DevWindowsMutexLease:
    def __init__(self, name):
        if os.name != "nt" or type(name) is not str or not _DEV_NAME.fullmatch(name):
            raise ValueError("Windows DEV-only Local mutex name required")
        self.name = name
        self._handle = None

    def try_acquire(self):
        """Return False for contention; never imply Owner authorization."""
        if self._handle is not None:
            raise DevLeaseError("already acquired")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel.ReleaseMutex.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateMutexW(None, False, self.name)
        if not handle:
            raise DevLeaseError("CreateMutexW failed")
        status = kernel.WaitForSingleObject(handle, 0)
        if status == _WAIT_TIMEOUT:
            kernel.CloseHandle(handle)
            return False
        if status == _WAIT_ABANDONED:
            # An abandoned lease is not a reason to replay old work.
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
            raise DevLeaseError("abandoned lease; outcome unknown, manual review")
        if status != _WAIT_OBJECT_0:
            kernel.CloseHandle(handle)
            raise DevLeaseError("mutex wait failed")
        self._handle = (kernel, handle)
        return True

    def close(self):
        if self._handle is None:
            return
        kernel, handle = self._handle
        self._handle = None
        released = kernel.ReleaseMutex(handle)
        closed = kernel.CloseHandle(handle)
        if not released or not closed:
            raise DevLeaseError("mutex release or close failed")

    def __enter__(self):
        if not self.try_acquire():
            raise DevLeaseError("another process holds DEV lease")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
