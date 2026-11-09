"""Fail-closed Windows 10/11 Owner session unlock state (local OS only).

WTSSessionInfoEx (25), WTSINFOEX_LEVEL1.SessionFlags:
  0 = LOCKED, 1 = UNLOCKED, -1 = UNKNOWN.
Never infer unlocked merely from WTSActive. No shell, Task, UI or network.
"""
import ctypes
from ctypes import wintypes
import os
import sys

if os.name != "nt":
    raise ImportError("Windows session lock verification requires Windows")

_wts=ctypes.WinDLL("wtsapi32",use_last_error=True)
_q=_wts.WTSQuerySessionInformationW
_q.restype=wintypes.BOOL
_q.argtypes=(wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,
             ctypes.POINTER(wintypes.LPVOID),ctypes.POINTER(wintypes.DWORD))
_free=_wts.WTSFreeMemory
_free.restype=None
_free.argtypes=(wintypes.LPVOID,)


class _Level1Head(ctypes.Structure):
    # The real structure contains 64-bit timestamps later on, therefore its
    # surrounding union is 8-byte aligned. Include a dummy 64-bit member
    # solely to express that layout; inspect only the first three fields.
    _fields_=[("session_id",wintypes.DWORD),
              ("connection_state",wintypes.DWORD),
              ("flags",wintypes.LONG),
              ("_force_8byte_alignment",ctypes.c_int64)]


class _LevelData(ctypes.Union):
    _fields_=[("head",_Level1Head)]


class _Info(ctypes.Structure):
    _fields_=[("level",wintypes.DWORD),("data",_LevelData)]


def decode_session_flags(blob, session_id):
    """Parse only trusted WTS API result; reject unknown/short/mismatch."""
    if type(session_id) is not int or session_id<=0:
        raise PermissionError("invalid Owner session")
    offset=_Info.data.offset
    if type(blob) is not bytes or len(blob)<ctypes.sizeof(_Info):
        raise PermissionError("truncated session details")
    header=_Info.from_buffer_copy(blob[:ctypes.sizeof(_Info)])
    if header.level!=1 or header.data.head.session_id!=session_id:
        raise PermissionError("unexpected Owner session details")
    if header.data.head.connection_state!=0:
        return False
    return header.data.head.flags==1


def session_is_unlocked(session_id):
    if type(session_id) is not int or session_id<=0:
        raise PermissionError("invalid session ID")
    if sys.getwindowsversion().major<10:
        raise PermissionError("unsupported Windows lock flag semantics")
    ptr=wintypes.LPVOID()
    size=wintypes.DWORD()
    if not _q(None,session_id,25,ctypes.byref(ptr),ctypes.byref(size)):
        raise PermissionError("WTS Owner lock state unavailable")
    try:
        if not ptr or size.value<ctypes.sizeof(_Info) or size.value>4096:
            raise PermissionError("malformed Owner lock state")
        return decode_session_flags(ctypes.string_at(ptr,size.value),session_id)
    finally:
        if ptr:
            _free(ptr)
