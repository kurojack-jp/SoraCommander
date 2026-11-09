"""Create missing local Owner storage at startup; never repair existing ACLs."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import stat


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


class _AttributeTag(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]


def _api(library, name, args, result):
    function = getattr(library, name)
    function.argtypes, function.restype = args, result
    return function


def _windows_api():
    if os.name != "nt":
        raise RuntimeError("Owner storage requires Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    return {
        "open": _api(kernel, "CreateFileW", [wintypes.LPCWSTR, wintypes.DWORD,
            wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
            wintypes.HANDLE], wintypes.HANDLE),
        "info": _api(kernel, "GetFileInformationByHandleEx", [wintypes.HANDLE,
            ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "close": _api(kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        "create": _api(kernel, "CreateDirectoryW", [wintypes.LPCWSTR,
            ctypes.POINTER(_SecurityAttributes)], wintypes.BOOL),
        "convert": _api(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
             ctypes.c_void_p], wintypes.BOOL),
        "free": _api(kernel, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p),
    }


@contextmanager
def _pin_parents(parent, api):
    """Open each ancestor without following reparse points or sharing deletion."""
    handles = []
    try:
        for path in (*reversed(parent.parents), parent):
            # FILE_READ_ATTRIBUTES; share read/write, excluding delete;
            # OPEN_EXISTING; BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
            handle = api["open"](str(path), 0x80, 3, None, 3, 0x02200000, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            info = _AttributeTag()
            if not api["info"](handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not info.attributes & 0x10 or info.attributes & 0x400:
                raise RuntimeError("Owner storage parent is not an ordinary directory")
        yield
    finally:
        failed = False
        for handle in reversed(handles):
            if not api["close"](handle):
                failed = True
        if failed:
            raise RuntimeError("Owner storage parent handle cleanup failed")


def _existing_directory(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise RuntimeError("Owner storage target is not an ordinary directory")
    return True


def _create_private_directory(path, api):
    from .windows_owner import current_windows_owner
    sid = current_windows_owner().sid
    if not re.fullmatch(r"S-1-\d+(?:-\d+)+", sid):
        raise RuntimeError("Owner storage identity unavailable")
    # Apply the protected three-principal DACL at creation, never by Set-Acl.
    sddl = f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    descriptor = ctypes.c_void_p()
    if not api["convert"](sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        if not api["create"](str(path), ctypes.byref(attributes)):
            error = ctypes.get_last_error()
            if error != 183:  # ERROR_ALREADY_EXISTS: no repair, no second create.
                raise ctypes.WinError(error)
            if not _existing_directory(path):
                raise RuntimeError("Owner storage creation race unresolved")
            return False
        if not _existing_directory(path):
            raise RuntimeError("Owner storage creation could not be verified")
        return True
    finally:
        api["free"](descriptor)


def ensure_owner_storage():
    """Create only a missing fixed child. Caller MUST run owner_journal_path next.

    Local launch environment only; no MCP arguments, alternate roots or retries.
    Existing folders and every existing ACL are left untouched, including on
    validation/creation failure. Never recursively create a missing parent.
    """
    value = os.environ.get("LOCALAPPDATA", "")
    parent = Path(value)
    if (not parent.is_absolute() or parent.drive.startswith("\\\\")
            or value.startswith(("\\\\", "//"))
            or any(part in (".", "..") for part in value.replace("\\", "/").split("/"))
            or any(":" in part for part in parent.parts[1:])):
        raise RuntimeError("Owner storage location invalid")
    path = parent / "SoraCommanderNext"
    api = _windows_api()
    with _pin_parents(parent, api):
        if _existing_directory(path):
            return False
        return _create_private_directory(path, api)
