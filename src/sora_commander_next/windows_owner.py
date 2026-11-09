"""Windows-only local logon identity observations; no trust in remote labels.

Win32 process TOKEN_USER / TOKEN_STATISTICS AuthenticationId / session and
WTS connect state. Rechecks at every Owner-only action. NOT user-presence
proof, protected IPC, or an isolation boundary against same-user malware.
"""
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import os

if os.name != "nt":
    raise ImportError("Owner Windows token binding requires Windows")

adv = ctypes.WinDLL("advapi32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
wts = ctypes.WinDLL("wtsapi32", use_last_error=True)

from .windows_session_lock import session_is_unlocked

def api(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.argtypes, fn.restype = args, result
    return fn

HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
BOOL = wintypes.BOOL

GetCurrentProcess = api(k32,"GetCurrentProcess",HANDLE)
GetCurrentThread = api(k32,"GetCurrentThread",HANDLE)
GetCurrentProcessId = api(k32,"GetCurrentProcessId",DWORD)
OpenProcessToken = api(adv,"OpenProcessToken",BOOL,HANDLE,DWORD,ctypes.POINTER(HANDLE))
OpenThreadToken = api(adv,"OpenThreadToken",BOOL,HANDLE,DWORD,BOOL,ctypes.POINTER(HANDLE))
GetTokenInformation = api(adv,"GetTokenInformation",BOOL,HANDLE,DWORD,
    wintypes.LPVOID,DWORD,ctypes.POINTER(DWORD))
ConvertSidToStringSidW = api(adv,"ConvertSidToStringSidW",BOOL,
    wintypes.LPVOID,ctypes.POINTER(wintypes.LPWSTR))
ProcessIdToSessionId = api(k32,"ProcessIdToSessionId",BOOL,
    DWORD,ctypes.POINTER(DWORD))
CloseHandle = api(k32,"CloseHandle",BOOL,HANDLE)
LocalFree = api(k32,"LocalFree",HANDLE,HANDLE)
WTSQuerySessionInformationW = api(wts,"WTSQuerySessionInformationW",BOOL,
    HANDLE,DWORD,DWORD,ctypes.POINTER(wintypes.LPVOID),ctypes.POINTER(DWORD))
WTSFreeMemory = api(wts,"WTSFreeMemory",None,wintypes.LPVOID)

class LUID(ctypes.Structure):
    _fields_=[("LowPart",DWORD),("HighPart",wintypes.LONG)]

class TokenStatistics(ctypes.Structure):
    _fields_=[("TokenId",LUID),("AuthenticationId",LUID),
              ("ExpirationTime",ctypes.c_int64),("TokenType",DWORD),
              ("ImpersonationLevel",DWORD),("DynamicCharged",DWORD),
              ("DynamicAvailable",DWORD),("GroupCount",DWORD),
              ("PrivilegeCount",DWORD),("ModifiedId",LUID)]

def check(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())

def token_bytes(handle, info_class):
    needed = DWORD()
    # Length probe must report ERROR_INSUFFICIENT_BUFFER=122, never trust
    # arbitrary nonzero sizes or return partial token information.
    result = GetTokenInformation(handle, info_class, None, 0, ctypes.byref(needed))
    if result or ctypes.get_last_error()!=122 or not 0 < needed.value <= 4096:
        raise PermissionError("Owner token information unavailable")
    buffer=ctypes.create_string_buffer(needed.value)
    check(GetTokenInformation(handle,info_class,buffer,needed,ctypes.byref(needed)))
    return buffer

@dataclass(frozen=True)
class WindowsOwnerIdentity:
    sid: str
    session_id: int
    logon_luid: tuple[int,int]
    active: bool

def current_windows_owner():
    """Inspect trusted local process and desktop session; fail closed."""
    thread_token=HANDLE()
    if OpenThreadToken(GetCurrentThread(),0x0008,True,ctypes.byref(thread_token)):
        CloseHandle(thread_token)
        raise PermissionError("thread impersonation cannot approve")
    if ctypes.get_last_error()!=1008:  # ERROR_NO_TOKEN
        raise PermissionError("unable to verify thread impersonation")
    process_token=HANDLE()
    check(OpenProcessToken(GetCurrentProcess(),0x0008,ctypes.byref(process_token)))
    try:
        user=token_bytes(process_token,1)  # TokenUser, SID_AND_ATTRIBUTES
        sid_pointer=ctypes.cast(user,ctypes.POINTER(ctypes.c_void_p))[0]
        sid_string=wintypes.LPWSTR()
        check(ConvertSidToStringSidW(sid_pointer,ctypes.byref(sid_string)))
        try:
            sid=sid_string.value
        finally:
            LocalFree(ctypes.cast(sid_string,HANDLE))
        stats=token_bytes(process_token,10)  # TokenStatistics
        if ctypes.sizeof(stats) < ctypes.sizeof(TokenStatistics):
            raise PermissionError("truncated token statistics")
        token=ctypes.cast(stats,ctypes.POINTER(TokenStatistics)).contents
        if token.TokenType!=1:
            raise PermissionError("primary Owner token required")
        luid=(token.AuthenticationId.LowPart,token.AuthenticationId.HighPart)
        session=DWORD()
        check(ProcessIdToSessionId(GetCurrentProcessId(),ctypes.byref(session)))
        if session.value==0 or sid in ("S-1-5-18","S-1-5-19","S-1-5-20"):
            raise PermissionError("service/builtin identity is not Owner")
        # Query Windows Terminal Services for this exact login session.
        ptr=wintypes.LPVOID()
        size=DWORD()
        check(WTSQuerySessionInformationW(None,session.value,8,
                                          ctypes.byref(ptr),ctypes.byref(size)))
        try:
            if size.value!=ctypes.sizeof(DWORD) or not ptr:
                raise PermissionError("invalid WTS state")
            state=ctypes.cast(ptr,ctypes.POINTER(DWORD)).contents.value
        finally:
            WTSFreeMemory(ptr)
        return WindowsOwnerIdentity(sid,session.value,luid,state==0)
    finally:
        CloseHandle(process_token)

class OwnerSessionGuard:
    """Pins explicit local Windows logon, not remotely provided strings."""

    def __init__(self):
        # Never accept a caller-supplied identity provider or remote SID.
        identity=current_windows_owner()
        if not isinstance(identity,WindowsOwnerIdentity) or not identity.active:
            raise PermissionError("active local Windows session required")
        if not session_is_unlocked(identity.session_id):
            raise PermissionError("Windows Owner desktop locked or unknown")
        self._identity=identity

    @property
    def sid(self):
        return self._identity.sid

    def verify(self):
        try:
            current=current_windows_owner()
        except Exception:
            raise PermissionError("Windows Owner identity unavailable") from None
        if current!=self._identity or not current.active:
            raise PermissionError("Windows Owner logon/session mismatch")
        try:
            unlocked=session_is_unlocked(current.session_id)
        except Exception:
            raise PermissionError("Windows Owner lock state unavailable") from None
        if not unlocked:
            raise PermissionError("Owner desktop locked, disconnected or unknown")
        return True

    def verify_process(self, pid):
        self.verify()
        try:
            other=identity_for_process_id(pid)
        except Exception:
            raise PermissionError("Owner UI child identity unavailable") from None
        if other!=self._identity or not other.active:
            raise PermissionError("Owner UI child logon/session mismatch")
        return True


OpenProcess = api(k32,"OpenProcess",HANDLE,DWORD,BOOL,DWORD)

def identity_for_process_id(pid):
    """Inspect a local child process token; fail closed, not a process signature."""
    if type(pid) is not int or pid <= 0:
        raise PermissionError("invalid Owner UI PID")
    handle=OpenProcess(0x1000,False,pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    check(handle)
    try:
        token_handle=HANDLE()
        check(OpenProcessToken(handle,0x0008,ctypes.byref(token_handle)))
        try:
            user=token_bytes(token_handle,1)
            sid_pointer=ctypes.cast(user,ctypes.POINTER(ctypes.c_void_p))[0]
            sid_string=wintypes.LPWSTR()
            check(ConvertSidToStringSidW(sid_pointer,ctypes.byref(sid_string)))
            try:
                sid=sid_string.value
            finally:
                LocalFree(ctypes.cast(sid_string,HANDLE))
            stats=token_bytes(token_handle,10)
            if ctypes.sizeof(stats)<ctypes.sizeof(TokenStatistics):
                raise PermissionError("truncated child token")
            st=ctypes.cast(stats,ctypes.POINTER(TokenStatistics)).contents
            if st.TokenType!=1:
                raise PermissionError("nonprimary child token")
            session=DWORD()
            check(ProcessIdToSessionId(pid,ctypes.byref(session)))
            if session.value==0 or sid in ("S-1-5-18","S-1-5-19","S-1-5-20"):
                raise PermissionError("service child not Owner")
            ptr=wintypes.LPVOID()
            size=DWORD()
            check(WTSQuerySessionInformationW(None,session.value,8,
                                              ctypes.byref(ptr),ctypes.byref(size)))
            try:
                if not ptr or size.value!=ctypes.sizeof(DWORD):
                    raise PermissionError("invalid child WTS state")
                active=ctypes.cast(ptr,ctypes.POINTER(DWORD)).contents.value==0
            finally:
                WTSFreeMemory(ptr)
            return WindowsOwnerIdentity(sid,session.value,
                (st.AuthenticationId.LowPart,st.AuthenticationId.HighPart),active)
        finally:
            CloseHandle(token_handle)
    finally:
        CloseHandle(handle)
