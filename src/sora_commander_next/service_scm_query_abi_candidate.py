"""B04-a Windows SCM query ABI candidate — inert until an explicit DLL is passed.

DEV_ONLY. Never loads a DLL, opens SCM, starts a service, or authenticates a
peer on import or construction. Intended to be injected into the previously
tested sample_scm_configuration backend. Fake Win32 functions are used in tests.

QueryServiceConfigW gives *registry* service configuration and may reflect the
next service start, not the currently running executable. Identical samples
do NOT establish process identity, publisher trust, or connected-pipe peer.
"""
import ctypes
from ctypes import c_uint32, c_int32, c_void_p, c_wchar_p, POINTER
from .service_scm_sampling_candidate import ScmSamplingError, SC_MANAGER_CONNECT, SERVICE_QUERY_CONFIG
from .offline_b04_service_identity_probe import SERVICE_NAME

DWORD = c_uint32
BOOL = c_int32
HANDLE = c_void_p
ERROR_INSUFFICIENT_BUFFER = 122
SERVICE_CONFIG_SERVICE_SID_INFO = 5
MAX_QUERY_BYTES = 8192
INVALID_HANDLE = c_void_p(-1).value


class QUERY_SERVICE_CONFIGW(ctypes.Structure):
    # winsvc.h QUERY_SERVICE_CONFIGW. LPWSTR fields are opaque pointers here:
    # Python's c_wchar_p would dereference before range-checking them.
    _fields_ = [
        ("dwServiceType", DWORD), ("dwStartType", DWORD),
        ("dwErrorControl", DWORD), ("lpBinaryPathName", c_void_p),
        ("lpLoadOrderGroup", c_void_p), ("dwTagId", DWORD),
        ("lpDependencies", c_void_p), ("lpServiceStartName", c_void_p),
        ("lpDisplayName", c_void_p),
    ]


class SERVICE_SID_INFO(ctypes.Structure):
    _fields_ = [("dwServiceSidType", DWORD)]


def _bind(dll, name, argtypes, restype):
    function = getattr(dll, name)
    function.argtypes = argtypes
    function.restype = restype
    return function


def _handle(value):
    if (type(value) is not int or value in (0, INVALID_HANDLE)
            or not 0 < value < (1 << (ctypes.sizeof(HANDLE) * 8))):
        raise ScmSamplingError("INVALID_NATIVE_HANDLE")
    return value


class ExplicitScmQueryBackend:
    """An explicit DLL + last-error adapter, NOT a WinDLL loader.

    Constructing from test doubles is safe. For any real WinDLL, an independent
    Owner-reviewed native acceptance gate MUST precede use: no automatic load,
    service lookup or trust upgrade is supplied by this module.
    """

    def __init__(self, advapi32, get_last_error):
        if not callable(get_last_error):
            raise ScmSamplingError("MISSING_LAST_ERROR_CALL")
        self._error = get_last_error
        self._open_scm = _bind(advapi32, "OpenSCManagerW",
                               [c_wchar_p, c_wchar_p, DWORD], HANDLE)
        self._open_service = _bind(advapi32, "OpenServiceW",
                                   [HANDLE, c_wchar_p, DWORD], HANDLE)
        self._query = _bind(advapi32, "QueryServiceConfigW",
                            [HANDLE, c_void_p, DWORD, POINTER(DWORD)], BOOL)
        self._query2 = _bind(advapi32, "QueryServiceConfig2W",
                             [HANDLE, DWORD, c_void_p, DWORD, POINTER(DWORD)], BOOL)
        self._close = _bind(advapi32, "CloseServiceHandle",
                            [HANDLE], BOOL)

    def _call_handle(self, function, *args):
        returned = function(*args)
        if returned in (None, 0, INVALID_HANDLE):
            raise ScmSamplingError("NATIVE_OPEN_FAILED")
        return _handle(returned)

    def open_scm(self, rights):
        if type(rights) is not int or rights != SC_MANAGER_CONNECT:
            raise ScmSamplingError("UNEXPECTED_SCM_ACCESS")
        return self._call_handle(self._open_scm, None, None, DWORD(rights))

    def open_service(self, scm, name, rights):
        _handle(scm)
        if name != SERVICE_NAME or type(rights) is not int or rights != SERVICE_QUERY_CONFIG:
            raise ScmSamplingError("UNEXPECTED_SERVICE_ACCESS")
        return self._call_handle(self._open_service, HANDLE(scm), name, DWORD(rights))

    def _bounded(self, *, service, second=False):
        """Probe then perform one bounded read. Never retry growing buffers."""
        _handle(service)
        needed = DWORD(0)
        if second:
            ok = self._query2(HANDLE(service), SERVICE_CONFIG_SERVICE_SID_INFO,
                              None, 0, ctypes.byref(needed))
        else:
            ok = self._query(HANDLE(service), None, 0, ctypes.byref(needed))
        error = self._error()
        minimum = ctypes.sizeof(SERVICE_SID_INFO if second else QUERY_SERVICE_CONFIGW)
        if ok != 0 or error != ERROR_INSUFFICIENT_BUFFER:
            raise ScmSamplingError("UNEXPECTED_SCM_PROBE")
        if not minimum <= needed.value <= MAX_QUERY_BYTES:
            raise ScmSamplingError("INVALID_SCM_BUFFER_BOUND")
        size = needed.value
        buffer = ctypes.create_string_buffer(size)
        needed2 = DWORD(0)
        if second:
            ok = self._query2(HANDLE(service), SERVICE_CONFIG_SERVICE_SID_INFO,
                              ctypes.cast(buffer, c_void_p), size, ctypes.byref(needed2))
        else:
            ok = self._query(HANDLE(service), ctypes.cast(buffer, c_void_p),
                             size, ctypes.byref(needed2))
        if ok == 0:
            # No second allocation even when config grew. An unstable sample
            # fails closed, preserving the caller's retained handle cleanup.
            raise ScmSamplingError("SCM_CONFIG_READ_FAILED")
        if needed2.value > size:
            raise ScmSamplingError("INCONSISTENT_SCM_BUFFER_SIZE")
        return buffer

    @staticmethod
    def _string(buffer, address):
        """Read only UTF-16LE NUL-terminated bytes INSIDE our owned buffer."""
        base = ctypes.addressof(buffer)
        maximum = ctypes.sizeof(buffer)
        if (type(address) is not int or address < base + ctypes.sizeof(QUERY_SERVICE_CONFIGW)
                or address >= base + maximum or (address - base) % 2):
            raise ScmSamplingError("SCM_STRING_POINTER_OUTSIDE_BUFFER")
        raw = ctypes.string_at(address, maximum - (address - base))
        stop = next((i for i in range(0, len(raw) - 1, 2) if raw[i:i+2] == b"\0\0"), -1)
        if stop < 2:
            raise ScmSamplingError("SCM_STRING_UNTERMINATED")
        try:
            result = raw[:stop].decode("utf-16-le", errors="strict")
        except UnicodeError as exc:
            raise ScmSamplingError("SCM_STRING_INVALID_UTF16") from exc
        if "\0" in result or len(result) > 4096:
            raise ScmSamplingError("SCM_STRING_INVALID")
        return result

    def query_config(self, service):
        buffer = self._bounded(service=service)
        view = QUERY_SERVICE_CONFIGW.from_buffer_copy(buffer)
        return (self._string(buffer, view.lpBinaryPathName),
                self._string(buffer, view.lpServiceStartName),
                int(view.dwServiceType), int(view.dwStartType),
                int(view.dwErrorControl))

    def query_sid_type(self, service):
        buffer = self._bounded(service=service, second=True)
        view = SERVICE_SID_INFO.from_buffer_copy(buffer)
        return int(view.dwServiceSidType)

    def close(self, handle):
        _handle(handle)
        return self._close(HANDLE(handle)) != 0
