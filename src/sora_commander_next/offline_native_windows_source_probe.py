"""DEV_ONLY native Windows read-handle evidence. No writer, no authority."""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from hashlib import sha256
import os
from .source_review import _literal

@dataclass(frozen=True, slots=True)
class WindowsReadEvidence:
    file_id: int
    volume_serial: int
    links: int
    attributes: int
    size: int
    digest: str
    authority: str = "NONE_READ_ONLY"

class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("created_low", wintypes.DWORD),
        ("created_high", wintypes.DWORD), ("accessed_low", wintypes.DWORD),
        ("accessed_high", wintypes.DWORD), ("written_low", wintypes.DWORD),
        ("written_high", wintypes.DWORD), ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD), ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD)]

def inspect_native_readonly(path, *, maximum=16384, while_held=None):
    """Open exact ordinary file exclusively and read bounded content.

    Evidence is diagnostic only; it cannot prove mapped-writer exclusion,
    ancestor stability, or atomic CAS.
    """
    if os.name != "nt":
        raise RuntimeError("Windows only")
    # Reject UNC, rooted-relative, device paths, ADS and noncanonical source names
    # before any Windows API invocation. No policy roots or write rights implied.
    _literal(path)
    if type(maximum) is not int or not 0 <= maximum <= 16384:
        raise ValueError("bounded read required")
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    dll.CreateFileW.restype = wintypes.HANDLE
    dll.GetFileInformationByHandle.argtypes = [wintypes.HANDLE,ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
    dll.GetFileInformationByHandle.restype = wintypes.BOOL
    dll.ReadFile.argtypes = [wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
    dll.ReadFile.restype = wintypes.BOOL
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL
    # FILE_READ_DATA | FILE_READ_ATTRIBUTES, share=0, OPEN_EXISTING,
    # FILE_FLAG_OPEN_REPARSE_POINT.
    handle = dll.CreateFileW(path, 0x81, 0, None, 3, 0x200000, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    failure = None
    try:
        info=BY_HANDLE_FILE_INFORMATION()
        if not dll.GetFileInformationByHandle(handle,ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        size=(int(info.size_high)<<32)|int(info.size_low)
        if (info.attributes & (0x400|0x10|0x2) or info.links != 1
                or size > maximum or not ((int(info.file_index_high)<<32)|int(info.file_index_low))):
            raise ValueError("unsupported native file identity")
        if while_held is not None:
            while_held()
        buffer=ctypes.create_string_buffer(maximum+1)
        count=wintypes.DWORD()
        if not dll.ReadFile(handle,buffer,maximum+1,ctypes.byref(count),None):
            raise ctypes.WinError(ctypes.get_last_error())
        data=bytes(buffer.raw[:count.value])
        if len(data)!=size or len(data)>maximum or bytes((0,)) in data:
            raise ValueError("native file changed or invalid bytes")
        data.decode("utf-8")
        after=BY_HANDLE_FILE_INFORMATION()
        if not dll.GetFileInformationByHandle(handle,ctypes.byref(after)):
            raise ctypes.WinError(ctypes.get_last_error())
        if any(getattr(info, key) != getattr(after, key) for key, _ in BY_HANDLE_FILE_INFORMATION._fields_ if key not in ("accessed_low", "accessed_high")):
            raise ValueError("native file identity changed")
        evidence=WindowsReadEvidence(
            (int(info.file_index_high)<<32)|int(info.file_index_low),
            int(info.volume),int(info.links),int(info.attributes),size,sha256(data).hexdigest())
    except BaseException as exc:
        failure=exc
    closed=dll.CloseHandle(handle)
    if not closed:
        raise OSError("native file handle cleanup unknown")
    if failure is not None:
        raise failure
    return evidence
