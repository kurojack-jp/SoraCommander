"""Windows named-pipe impersonated client token verification (no listener).

The pipe server must call ImpersonateNamedPipeClient on a connected, already
read request, then invoke verify_impersonated_client on that SAME thread.
The server must always RevertToSelf in a finally block before dispatch.
This module does not create a pipe, accept a connection, or authorize launch.
"""
import ctypes
from ctypes import wintypes
import os

if os.name != "nt":
    raise ImportError("Windows-only impersonation token verification")

from .windows_owner import (
    GetCurrentThread, OpenThreadToken, CloseHandle, token_bytes,
    ConvertSidToStringSidW, LocalFree, TokenStatistics, WindowsOwnerIdentity,
)

TOKEN_QUERY = 0x0008
TOKEN_SESSION_ID = 12


def verify_impersonated_client(expected: WindowsOwnerIdentity):
    """Require actual thread impersonation token to match pinned Owner logon.

    Caller must verify server Owner/unlocked state before impersonating and
    again after reverting; never take expected identity from an IPC payload.
    """
    return _verify_thread_client(expected, levels=(2, 3))


def verify_identified_ledger_client(expected: WindowsOwnerIdentity):
    """New ledger endpoint ONLY: require exact SecurityIdentification level.

    Does not relax the Owner Broker verifier. A future service must derive the
    registered Owner/session binding independently, read from this connection,
    impersonate on this same thread and always revert before dispatch. This
    function neither creates that service nor authorizes a human approval.
    """
    return _verify_thread_client(expected, levels=(1,))


def _verify_thread_client(expected, *, levels):
    if type(expected) is not WindowsOwnerIdentity or expected.active is not True:
        raise PermissionError("pinned active Owner identity required")
    handle = wintypes.HANDLE()
    if not OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, True, ctypes.byref(handle)):
        raise PermissionError("missing impersonated client token")
    try:
        user = token_bytes(handle, 1)
        if ctypes.sizeof(user) < ctypes.sizeof(ctypes.c_void_p) + ctypes.sizeof(wintypes.DWORD):
            raise PermissionError("truncated client token user")
        sid_pointer = ctypes.cast(user, ctypes.POINTER(ctypes.c_void_p))[0]
        if not sid_pointer:
            raise PermissionError("missing client SID")
        sid_string = wintypes.LPWSTR()
        if not ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_string)):
            raise PermissionError("client SID unavailable")
        try:
            sid = sid_string.value
        finally:
            LocalFree(ctypes.cast(sid_string, wintypes.HANDLE))
        stats = token_bytes(handle, 10)
        if ctypes.sizeof(stats) < ctypes.sizeof(TokenStatistics):
            raise PermissionError("truncated client token statistics")
        st = ctypes.cast(stats, ctypes.POINTER(TokenStatistics)).contents
        # Endpoint-specific levels; never accept a lower-level token as an
        # implicit fallback, or grant a ledger server Owner impersonation.
        if st.TokenType != 2 or st.ImpersonationLevel not in levels:
            raise PermissionError("client token level does not match endpoint")
        session_raw = token_bytes(handle, TOKEN_SESSION_ID)
        if ctypes.sizeof(session_raw) != ctypes.sizeof(wintypes.DWORD):
            raise PermissionError("invalid client token session")
        session = ctypes.cast(session_raw, ctypes.POINTER(wintypes.DWORD)).contents.value
        actual = (sid, session, (st.AuthenticationId.LowPart, st.AuthenticationId.HighPart))
        pinned = (expected.sid, expected.session_id, expected.logon_luid)
        if actual != pinned:
            raise PermissionError("client SID/session/logon mismatch")
        return True
    finally:
        CloseHandle(handle)
