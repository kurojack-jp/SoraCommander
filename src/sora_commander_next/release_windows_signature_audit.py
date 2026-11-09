"""Read-only Windows embedded Authenticode observation, always HOLD_LIVE.

Only WTD_CHOICE_FILE embedded verification is implemented. Catalog-only
signatures are unsupported; explicit catalog verification needs separate work.
Offline Windows chain policy acceptance is neither a publisher pin nor fresh
revocation evidence. Supply exact size/SHA256 through an independent channel;
this observer cannot authenticate that channel and never grants installation
or Broker activation. No signer, trust store, execution, Host or IPC changes.

API layouts/semantics: Microsoft Learn wintrust.h WINTRUST_DATA,
WINTRUST_FILE_INFO, WinVerifyTrust; fileapi.h CreateFileW and
GetFileInformationByHandleEx. Native alignment, never packed structures.
"""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import hashlib
import hmac
import ntpath
from pathlib import Path
import sys

DEFAULT_MAX_BYTES = 128 * 1024 * 1024
# Explicit override accommodates the known ~325MB Codex fixture; never unbounded.
MAX_BYTES = 384 * 1024 * 1024
_CHUNK = 1024 * 1024
_REPARSE = 0x400
_DIRECTORY = 0x10
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class WindowsSignatureAuditError(ValueError):
    """No accepted evidence can be produced; no execution is authorized."""


@dataclass(frozen=True, slots=True)
class EmbeddedSignatureEvidence:
    path: str
    sha256: str
    size: int
    volume_serial_number: int
    file_id: str
    last_write_time: int
    link_count: int
    winverifytrust_status: int = field(default=0, init=False)
    state_close_status: int = field(default=0, init=False)
    windows_chain_policy_accepted: bool = field(default=True, init=False)
    publisher_pinned: bool = field(default=False, init=False)
    revocation_fresh: bool = field(default=False, init=False)
    code_provenance_authenticated: bool = field(default=False, init=False)
    external_pin_origin_verified: bool = field(default=False, init=False)
    install_permitted: bool = field(default=False, init=False)
    broker_activation_permitted: bool = field(default=False, init=False)
    embedded_only: bool = field(default=True, init=False)
    catalog_supported: bool = field(default=False, init=False)
    status: str = field(default="HOLD_LIVE", init=False)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR),
                ("hFile", wintypes.HANDLE), ("pgKnownSubject", ctypes.POINTER(GUID))]


class WINTRUST_DATA(ctypes.Structure):
    # Only the FILE union member is used; all union arms are pointer-sized.
    _fields_ = [("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
                ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
                ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
                ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)),
                ("dwStateAction", wintypes.DWORD), ("hWVTStateData", wintypes.HANDLE),
                ("pwszURLReference", wintypes.LPWSTR), ("dwProvFlags", wintypes.DWORD),
                ("dwUIContext", wintypes.DWORD), ("pSignatureSettings", ctypes.c_void_p)]


class _FileInformation(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
                ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
                ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]


class _FileId(ctypes.Structure):
    _fields_ = [("volume", ctypes.c_ulonglong), ("identifier", ctypes.c_ubyte * 16)]


class _Windows:
    def __init__(self):
        if sys.platform != "win32":
            raise WindowsSignatureAuditError("Windows trust APIs required")
        # LOAD_LIBRARY_SEARCH_SYSTEM32: do not load a DLL from a candidate path.
        self.kernel = ctypes.WinDLL("kernel32.dll", winmode=0x800)
        self.trust = ctypes.WinDLL("wintrust.dll", winmode=0x800)
        declarations = (
            (self.kernel.CreateFileW, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
             ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
            (self.kernel.CloseHandle, [wintypes.HANDLE], wintypes.BOOL),
            (self.kernel.GetDriveTypeW, [wintypes.LPCWSTR], wintypes.UINT),
            (self.kernel.GetFileType, [wintypes.HANDLE], wintypes.DWORD),
            (self.kernel.GetFileInformationByHandle,
             [wintypes.HANDLE, ctypes.POINTER(_FileInformation)], wintypes.BOOL),
            (self.kernel.GetFileInformationByHandleEx,
             [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            (self.kernel.GetFinalPathNameByHandleW,
             [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD], wintypes.DWORD),
            (self.kernel.SetFilePointerEx,
             [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD], wintypes.BOOL),
            (self.kernel.ReadFile, [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
            (self.trust.WinVerifyTrust,
             [wintypes.HWND, ctypes.POINTER(GUID), ctypes.c_void_p], wintypes.LONG),
        )
        for function, args, result in declarations:
            function.argtypes = args
            function.restype = result


def _path(path):
    if not isinstance(path, (str, Path)):
        raise WindowsSignatureAuditError("ordinary absolute local binary path required")
    raw = str(path)
    if (len(raw) < 7 or len(raw) > 259 or not raw[0].isascii()
            or not raw[0].isalpha() or raw[1:3] != ":\\"
            or any(c in raw[2:] for c in '/:*?"<>|')
            or any(ord(c) < 32 for c in raw)):
        raise WindowsSignatureAuditError("ordinary absolute local binary path required")
    reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    reserved.update(f"{p}{n}" for p in ("COM", "LPT") for n in "123456789¹²³")
    parts = raw[3:].split("\\")
    if any(not p or p in (".", "..") or p.endswith((".", " "))
           or p.split(".")[0].upper() in reserved for p in parts):
        raise WindowsSignatureAuditError("dot, empty, ambiguous or device path component")
    if ntpath.splitext(raw)[1].lower() not in (".exe", ".dll", ".pyd"):
        raise WindowsSignatureAuditError("expected .exe/.dll/.pyd")
    return raw


def _open(api, path, handles, *, directory=False):
    # No WRITE/DELETE access. Parents cannot be renamed; target additionally
    # denies write sharing for the entire hash/VERIFY/CLOSE/rehash interval.
    handle = api.kernel.CreateFileW(path, 0x80 if directory else 0x80000000,
                                   3 if directory else 1, None, 3,
                                   0x00200000 | (0x02000000 if directory else 0), None)
    if handle in (None, 0, _INVALID_HANDLE):
        raise WindowsSignatureAuditError("read-only file open failed")
    handles.append(handle)
    info = _FileInformation()
    if not api.kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise WindowsSignatureAuditError("file metadata unavailable")
    if info.attributes & _REPARSE or bool(info.attributes & _DIRECTORY) != directory:
        raise WindowsSignatureAuditError(f"reparse or nonordinary file/parent: {path}")
    if api.kernel.GetFileType(handle) != 1:  # FILE_TYPE_DISK
        raise WindowsSignatureAuditError("ordinary disk file required")
    buffer = ctypes.create_unicode_buffer(32768)
    count = api.kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if not 0 < count < len(buffer):
        raise WindowsSignatureAuditError("final file path unavailable")
    final = buffer.value
    if not final.startswith("\\\\?\\") or final[4:].rstrip("\\").casefold() != path.rstrip("\\").casefold():
        raise WindowsSignatureAuditError("redirected or nonordinary final path")
    return handle


def _snapshot(api, handle):
    info, identity = _FileInformation(), _FileId()
    if (not api.kernel.GetFileInformationByHandle(handle, ctypes.byref(info))
            or not api.kernel.GetFileInformationByHandleEx(handle, 18, ctypes.byref(identity), ctypes.sizeof(identity))):
        raise WindowsSignatureAuditError("file identity unavailable")
    if info.attributes & (_REPARSE | _DIRECTORY) or info.links != 1:
        raise WindowsSignatureAuditError("ordinary file with exactly one link required")
    return (identity.volume, bytes(identity.identifier).hex(),
            (info.size_high << 32) | info.size_low,
            (info.write.dwHighDateTime << 32) | info.write.dwLowDateTime, info.links)


def _hash(api, handle, size, limit):
    if not api.kernel.SetFilePointerEx(handle, 0, None, 0):
        raise WindowsSignatureAuditError("file seek failed")
    digest, total = hashlib.sha256(), 0
    buffer, count = ctypes.create_string_buffer(_CHUNK), wintypes.DWORD()
    while True:
        # One sentinel byte at most beyond the exact pin; never unbounded reads.
        amount = min(_CHUNK, size + 1 - total)
        if not api.kernel.ReadFile(handle, buffer, amount, ctypes.byref(count), None):
            raise WindowsSignatureAuditError("file read failed")
        if count.value > amount:
            raise WindowsSignatureAuditError("invalid file read result")
        if count.value == 0:
            break
        total += count.value
        if total > size or total > limit:
            raise WindowsSignatureAuditError("file size changed or bound exceeded")
        digest.update(buffer.raw[:count.value])
    if total != size:
        raise WindowsSignatureAuditError("file size changed")
    return digest.hexdigest()


def _status(value):
    # WinVerifyTrust returns a signed LONG status, NOT GetLastError nor a BOOL.
    if type(value) is not int or not -(1 << 31) <= value <= 0xffffffff:
        raise WindowsSignatureAuditError("invalid WinVerifyTrust status")
    return ctypes.c_int32(value).value


def _verify(api, path, handle):
    action = GUID(0x00AAC56B, 0xCD44, 0x11D0,
                  (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    file_info = WINTRUST_FILE_INFO(ctypes.sizeof(WINTRUST_FILE_INFO), path, handle, None)
    data = WINTRUST_DATA()
    data.cbStruct = ctypes.sizeof(data)
    data.dwUIChoice = 2  # WTD_UI_NONE
    data.fdwRevocationChecks = 0  # WTD_REVOKE_NONE
    data.dwUnionChoice = 1  # WTD_CHOICE_FILE; never catalog fallback
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = 1  # WTD_STATEACTION_VERIFY
    data.dwProvFlags = 0x1000 | 0x10  # CACHE_ONLY_URL_RETRIEVAL | REVOCATION_CHECK_NONE
    try:
        status = _status(api.trust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data)))
        if status != 0:
            raise WindowsSignatureAuditError(f"WinVerifyTrust rejected embedded signature: {status} (0x{status & 0xffffffff:08X})")
    finally:
        data.dwStateAction = 2  # WTD_STATEACTION_CLOSE, including VERIFY failure
        closed = _status(api.trust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data)))
        if closed != 0:
            raise WindowsSignatureAuditError(f"WinVerifyTrust state CLOSE failed: {closed}")


def inspect_embedded_signature(path, *, expected_sha256, expected_size,
                               max_bytes=DEFAULT_MAX_BYTES):
    """Return frozen HOLD evidence only after exact pins, offline trust and CLOSE.

    File/parent handles stay open through the final identity check and rehash.
    Python lstat/fstat creation times are deliberately never compared.
    An explicit larger bound is needed for files above the 128MiB default;
    even overrides cannot exceed 384MiB. Errors always deny evidence.
    """
    raw = _path(path)
    if (type(expected_sha256) is not str or len(expected_sha256) != 64
            or any(c not in "0123456789abcdef" for c in expected_sha256)):
        raise WindowsSignatureAuditError("exact lowercase SHA256 pin required")
    if (type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES
            or type(expected_size) is not int or not 0 < expected_size <= max_bytes):
        raise WindowsSignatureAuditError("exact positive size pin within byte bound required")
    handles = []
    try:
        api = _Windows()
        if api.kernel.GetDriveTypeW(raw[:3]) != 3:  # DRIVE_FIXED only
            raise WindowsSignatureAuditError("local fixed drive required")
        _open(api, raw[:3], handles, directory=True)
        parent = raw[:3]
        for part in raw[3:].split("\\")[:-1]:
            parent = ntpath.join(parent, part)
            _open(api, parent, handles, directory=True)
        handle = _open(api, raw, handles)
        before = _snapshot(api, handle)
        if before[2] != expected_size:
            raise WindowsSignatureAuditError("size pin mismatch")
        digest = _hash(api, handle, expected_size, max_bytes)
        if not hmac.compare_digest(digest, expected_sha256):
            raise WindowsSignatureAuditError("SHA256 pin mismatch")
        if _snapshot(api, handle) != before:
            raise WindowsSignatureAuditError("file changed while hashing")
        _verify(api, raw, handle)
        # Reopen only AFTER provider CLOSE: Windows may lock the file in VERIFY.
        current = _open(api, raw, handles)
        if _snapshot(api, current) != before or _snapshot(api, handle) != before:
            raise WindowsSignatureAuditError("file identity or metadata changed during trust verification")
        if not hmac.compare_digest(_hash(api, handle, expected_size, max_bytes), expected_sha256):
            raise WindowsSignatureAuditError("file content changed during trust verification")
        if _snapshot(api, current) != before or _snapshot(api, handle) != before:
            raise WindowsSignatureAuditError("file changed during final rehash")
        evidence = EmbeddedSignatureEvidence(raw, digest, expected_size,
                                             before[0], before[1], before[3], before[4])
    except (OSError, AttributeError, ctypes.ArgumentError) as exc:
        raise WindowsSignatureAuditError("Windows signature observation failed") from exc
    finally:
        close_failed = False
        for opened in reversed(handles):
            try:
                if not api.kernel.CloseHandle(opened):
                    close_failed = True
            except (OSError, AttributeError, ctypes.ArgumentError):
                close_failed = True
        if close_failed:
            raise WindowsSignatureAuditError("read-only handle CLOSE failed")
    return evidence
