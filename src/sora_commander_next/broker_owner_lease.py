"""Windows Owner-logon singleton primitive; importing never acquires a lease.

This is kernel name exclusion, NOT caller authentication, code provenance,
same-user malware isolation, or durable replay protection. A same-user process
can precreate the predictable name and deny service. HOLD_LIVE: a protected
signed Broker, trusted installation/state ACLs, independent protected persistent
ledger, Owner GUI and SID/LUID/WTS-authenticated IPC remain unimplemented or
unaccepted. No listener, execution authority, or activation is provided here.
"""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import threading

if os.name != "nt":
    raise ImportError("Broker Owner lease requires Windows")

from .windows_owner import OwnerSessionGuard, WindowsOwnerIdentity

_GUARD_TYPE = OwnerSessionGuard
_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi = ctypes.WinDLL("advapi32", use_last_error=True)
_ERROR_ALREADY_EXISTS = 183


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL)]


def _api(dll, name, result, *args):
    function = getattr(dll, name)
    function.restype, function.argtypes = result, args
    return function


_CreateMutexW = _api(_kernel, "CreateMutexW", wintypes.HANDLE,
                     ctypes.POINTER(_SecurityAttributes), wintypes.BOOL,
                     wintypes.LPCWSTR)
_ReleaseMutex = _api(_kernel, "ReleaseMutex", wintypes.BOOL, wintypes.HANDLE)
_CloseHandle = _api(_kernel, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_LocalFree = _api(_kernel, "LocalFree", wintypes.HANDLE, wintypes.HANDLE)
_GetCurrentThreadId = _api(_kernel, "GetCurrentThreadId", wintypes.DWORD)
_ConvertSDDL = _api(
    _advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", wintypes.BOOL,
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.DWORD))


class BrokerOwnerLeaseError(PermissionError):
    """Lease denied or cleanup failed; never proceed as a Broker authority."""


def _dispose(handle, owned):
    # Close even if ReleaseMutex fails. No retry and no subsequent handle use.
    try:
        if owned and not _ReleaseMutex(handle):
            raise BrokerOwnerLeaseError("Owner mutex release failed")
    finally:
        if not _CloseHandle(handle):
            raise BrokerOwnerLeaseError("Owner mutex handle close failed")


class BrokerOwnerLease:
    """Single-use context manager, acquired and closed on the same OS thread.

    Construction pins a real local OwnerSessionGuard; no caller-provided SID,
    guard, name, environment override, waiting, force, or automatic retry.
    The guard is checked on entry and immediately after creation/SD cleanup.
    Later session changes require fresh guard checks by the future Broker;
    this primitive does not provide continuous lock monitoring.
    """

    def __init__(self):
        self._guard = OwnerSessionGuard()
        if type(self._guard) is not _GUARD_TYPE:
            raise TypeError("actual OwnerSessionGuard required")
        self._identity = self._guard._identity
        if type(self._identity) is not WindowsOwnerIdentity:
            raise BrokerOwnerLeaseError("pinned Windows Owner identity required")
        self._verify()
        identity = self._identity
        binding = json.dumps(
            [identity.sid, *identity.logon_luid, identity.session_id],
            ensure_ascii=True, separators=(",", ":")).encode("ascii")
        self._name = "Local\\SoraCommanderNext.BrokerOwnerLease.v1." + (
            hashlib.sha256(binding).hexdigest())
        self._handle = None
        self._thread_id = None
        self._attempted = False
        self._closed = False
        self._lock = threading.Lock()

    def _verify(self):
        if (type(self._guard) is not _GUARD_TYPE
                or self._guard._identity != self._identity
                or _GUARD_TYPE.verify(self._guard) is not True
                or self._guard._identity != self._identity):
            raise BrokerOwnerLeaseError("Owner logon verification failed")

    def __enter__(self):
        with self._lock:
            if self._attempted or self._closed:
                raise BrokerOwnerLeaseError("Owner lease is single-use")
            self._attempted = True
            handle = None
            owned = False
            descriptor = wintypes.LPVOID()
            try:
                self._verify()
                # Protected, non-NULL DACL: SYSTEM, Administrators, exact Owner.
                # This descriptor applies ONLY when Windows creates a new name.
                sddl = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;" + self._identity.sid + ")"
                try:
                    size = wintypes.DWORD()
                    if not _ConvertSDDL(sddl, 1, ctypes.byref(descriptor),
                                        ctypes.byref(size)):
                        raise BrokerOwnerLeaseError("Owner mutex SDDL conversion failed")
                    if not descriptor or size.value < 20:
                        raise BrokerOwnerLeaseError("Owner mutex descriptor unavailable")
                    attributes = _SecurityAttributes(
                        ctypes.sizeof(_SecurityAttributes), descriptor, False)
                    ctypes.set_last_error(0)
                    handle = _CreateMutexW(ctypes.byref(attributes), True, self._name)
                    # Capture before LocalFree or any other Win32 call.
                    error = ctypes.get_last_error()
                    if not handle:
                        raise BrokerOwnerLeaseError("Owner mutex creation denied")
                    if error == _ERROR_ALREADY_EXISTS:
                        # Existing objects are denied even when unused, abandoned,
                        # recursively owned by this thread, or differently secured.
                        raise BrokerOwnerLeaseError("Owner mutex name already exists")
                    owned = True
                    if error:
                        raise BrokerOwnerLeaseError("unexpected Owner mutex OS status")
                finally:
                    if descriptor and _LocalFree(descriptor):
                        raise BrokerOwnerLeaseError("Owner mutex descriptor free failed")
                self._verify()
                self._thread_id = _GetCurrentThreadId()
                self._handle = handle
                return self
            except BaseException:
                self._closed = True
                if handle:
                    _dispose(handle, owned)
                raise

    def close(self):
        with self._lock:
            if self._handle is None:
                self._closed = True
                return
            # A mutex is owned by its acquiring thread. Preserve the handle for
            # that thread's cleanup if a different thread incorrectly calls us.
            if _GetCurrentThreadId() != self._thread_id:
                raise BrokerOwnerLeaseError("Owner lease must close on acquiring thread")
            handle, self._handle = self._handle, None
            self._closed = True
            _dispose(handle, True)

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False
