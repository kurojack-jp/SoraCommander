"""DEV_ONLY native evidence gathering, NEVER authenticated service admission.

Uses the B04-a retained process observation, not a second PID observer. No
connection is made. A connected CLIENT pipe HANDLE is borrowed, never closed
or queried: GetNamedPipeServerProcessId CLIENT semantics are NOT_ACCEPTED.
Image/token observations cannot establish independent Publisher/Trust or bind
the mapped executable to an on-disk file. No asserted trusted=True input exists.
"""
import ctypes as C
from dataclasses import dataclass, field
import os
from pathlib import PureWindowsPath
import re
import sys

from . import offline_b04_service_identity_probe as base

DWORD, BOOL, HANDLE = base.DWORD, base.BOOL, base.HANDLE
TOKEN_QUERY = 0x0008
IMAGE_ACCESS = 0x0080  # FILE_READ_ATTRIBUTES only; no signature/content read yet.
MISSING_EVIDENCE = (
    "CLIENT_HANDLE_PID_CONTRACT_NOT_ACCEPTED",
    "CONNECTION_EPOCH_AND_LIFETIME_NOT_OS_VERIFIED",
    "SCM_CONFIG_AND_REGISTERED_RESTRICTED_SERVICE_SID_UNVERIFIED",
    "IMAGE_ANCESTOR_OWNER_DACL_AND_MAPPED_IMAGE_BINDING_UNVERIFIED",
    "INDEPENDENT_INSTALLER_PUBLISHER_PIN_AND_RELEASE_GENERATION_UNKNOWN",
    "WINVERIFYTRUST_AND_PROTECTED_RELEASE_BYTES_NOT_IMPLEMENTED",
    "PIPE_DACL_SQOS_LOCALITY_UNVERIFIED",
    "RETAINED_PEER_IDENTITY_THROUGH_REPLY_NOT_IMPLEMENTED",
)


class FILE_ID_INFO(C.Structure):
    _fields_ = [("volume", C.c_uint64), ("identifier", C.c_ubyte * 16)]


class FILE_ATTRIBUTE_TAG_INFO(C.Structure):
    _fields_ = [("attributes", DWORD), ("tag", DWORD)]


class FILE_STANDARD_INFO(C.Structure):
    _fields_ = [("allocation", C.c_int64), ("size", C.c_int64),
                ("links", DWORD), ("delete_pending", C.c_ubyte),
                ("directory", C.c_ubyte)]


class SID_AND_ATTRIBUTES(C.Structure):
    _fields_ = [("sid", C.c_void_p), ("attributes", DWORD)]


class TOKEN_GROUPS_HEAD(C.Structure):
    _fields_ = [("count", DWORD), ("first", SID_AND_ATTRIBUTES)]


@dataclass(frozen=True, slots=True)
class NativePrerequisites:
    process: base.ServiceProcessObservation
    image_path: str
    image_identity: tuple
    token_user: str
    service_groups: tuple
    restricted_service_groups: tuple
    connection_epoch: int  # Local correlation label, NOT a native proof.
    authentication: str = field(default="NOT_AUTHENTICATED", init=False)
    admission: str = field(default="UNAVAILABLE", init=False)
    missing_evidence: tuple = field(default=MISSING_EVIDENCE, init=False)


class _NativeCalls:
    def __init__(self, advapi32, kernel32, last_error):
        self.base = base._NativeCalls(advapi32, kernel32, last_error)
        self.last_error = last_error

        def bind(dll, name, args, result):
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result
            return fn

        self.image_name = bind(kernel32, "QueryFullProcessImageNameW",
            [HANDLE, DWORD, C.c_wchar_p, C.POINTER(DWORD)], BOOL)
        self.open_image = bind(kernel32, "CreateFileW",
            [C.c_wchar_p, DWORD, DWORD, C.c_void_p, DWORD, DWORD, HANDLE], HANDLE)
        self.file_info = bind(kernel32, "GetFileInformationByHandleEx",
            [HANDLE, C.c_int32, C.c_void_p, DWORD], BOOL)
        self.final_path = bind(kernel32, "GetFinalPathNameByHandleW",
            [HANDLE, C.c_wchar_p, DWORD, DWORD], DWORD)
        self.open_token = bind(advapi32, "OpenProcessToken",
            [HANDLE, DWORD, C.POINTER(HANDLE)], BOOL)
        self.token_info = bind(advapi32, "GetTokenInformation",
            [HANDLE, C.c_int32, C.c_void_p, DWORD, C.POINTER(DWORD)], BOOL)
        self.close = self.base.close_process


def _load_native():
    if os.name != "nt" or sys.maxsize <= 2**32:
        raise base.ObservationError("PLATFORM_UNSUPPORTED", "native_prerequisites.bind")
    return _NativeCalls(
        C.WinDLL("advapi32.dll", use_last_error=True, winmode=0x800),
        C.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800), C.get_last_error)


def _reject(stage):
    raise base.ObservationError("PREREQUISITE_MISMATCH", stage)


def _path(value):
    # Conservative DOS absolute spelling only; no UNC/device/ADS/short names.
    if (type(value) is not str or not 3 < len(value) < 32768
            or re.fullmatch(r"[A-Za-z]:\\.*", value) is None
            or str(PureWindowsPath(value)) != value):
        _reject("image_path")
    for part in value[3:].split("\\"):
        if (not part or part in (".", "..") or part.endswith((".", " "))
                or "~" in part or any(ord(ch) < 32 or ch in '<>:"/|?*' for ch in part)
                or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", part)):
            _reject("image_path")
    return value


def _sid_at(buffer, used, pointer, minimum_offset):
    """Check ALL pointer bounds before dereferencing variable native SID data."""
    start = C.addressof(buffer)
    if type(pointer) is not int or not start + minimum_offset <= pointer <= start + used - 8:
        _reject("token_sid_pointer")
    offset = pointer - start
    head = bytes(buffer[offset:offset + 8])
    count = head[1]
    if head[0] != 1 or not 1 <= count <= 15 or offset + 8 + count * 4 > used:
        _reject("token_sid_bounds")
    data = bytes(buffer[offset + 8:offset + 8 + count * 4])
    return "S-1-" + str(int.from_bytes(head[2:], "big")) + "".join(
        "-" + str(int.from_bytes(data[i:i + 4], "little")) for i in range(0, len(data), 4))


def _token_sample(api, token, check):
    def query(kind, minimum):
        # One fixed allocation/query, no unbounded resize or retry on error122.
        buf, used = C.create_string_buffer(65536), DWORD()
        base._boolean(api, api.token_info(token, kind, buf, len(buf), C.byref(used)),
                      "GetTokenInformation")
        check()
        if not minimum <= used.value <= len(buf):
            _reject("token_return_length")
        return buf, used.value

    buf, used = query(8, 4)  # TokenType must be TokenPrimary.
    if used != 4 or DWORD.from_buffer(buf).value != 1:
        _reject("token_type")
    buf, used = query(1, C.sizeof(SID_AND_ATTRIBUTES))
    user = _sid_at(buf, used, SID_AND_ATTRIBUTES.from_buffer(buf).sid,
                   C.sizeof(SID_AND_ATTRIBUTES))
    if user != "S-1-5-19":
        _reject("token_user_not_local_service")

    def groups(kind):
        offset = TOKEN_GROUPS_HEAD.first.offset
        buf, used = query(kind, offset)
        count = DWORD.from_buffer(buf).value
        if count > 256 or offset + count * C.sizeof(SID_AND_ATTRIBUTES) > used:
            _reject("token_group_count")
        result = []
        for index in range(count):
            group = SID_AND_ATTRIBUTES.from_buffer(buf, offset + index * C.sizeof(SID_AND_ATTRIBUTES))
            sid = _sid_at(buf, used, group.sid, offset + count * C.sizeof(SID_AND_ATTRIBUTES))
            if sid.startswith("S-1-5-80-"):
                # Normal groups must be enabled and not deny-only. Do not
                # invent enabled-bit semantics for TokenRestrictedSids: record
                # those attributes verbatim, require SID presence separately.
                if kind == 2 and (not group.attributes & 4 or group.attributes & 0x10):
                    _reject("service_sid_disabled_or_deny_only")
                result.append((sid, group.attributes))
        if not result or len({sid for sid, _ in result}) != len(result):
            _reject("service_sid_missing_or_duplicate")
        return tuple(sorted(result))

    enabled, restricted = groups(2), groups(11)
    if {sid for sid, _ in enabled} != {sid for sid, _ in restricted}:
        _reject("restricted_service_sid_mismatch")
    # These SID candidates are not matched to protected installer registration.
    return user, enabled, restricted


def observe_connected_service_prerequisites(client_handle, *, connection_epoch,
                                            timeout_ms=1000, _calls=None):
    """Read native prerequisites only. Caller owns its connected client HANDLE.

    No path from this function to AUTHENTICATED exists, even for perfect mock
    results. Never send ledger traffic based on this ordinary Python record.
    """
    for value, maximum in ((client_handle, base.INVALID_HANDLE - 16),
                           (connection_epoch, (1 << 63) - 1)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise base.ObservationError("INVALID_INPUT", "borrowed_connection")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 5000:
        raise base.ObservationError("INVALID_INPUT", "timeout_ms")
    if _calls is not None and type(_calls) is not _NativeCalls:
        raise base.ObservationError("INVALID_INPUT", "native_calls")
    collected = []

    def inspect(process, check):
        api = holder[0]
        owned = [None, None]
        primary = None

        def image_name():
            buf, count = C.create_unicode_buffer(32768), DWORD(32768)
            base._boolean(api, api.image_name(process, 0, buf, C.byref(count)),
                          "QueryFullProcessImageNameW")
            check()
            if not 3 < count.value < len(buf) or buf[count.value] != "\0":
                _reject("image_name_length")
            return _path(buf[:count.value])

        def file_sample(image):
            records = []
            for kind, cls in ((18, FILE_ID_INFO), (9, FILE_ATTRIBUTE_TAG_INFO),
                              (1, FILE_STANDARD_INFO)):
                value = cls()
                base._boolean(api, api.file_info(image, kind, C.byref(value), C.sizeof(value)),
                              "GetFileInformationByHandleEx")
                check()
                records.append(value)
            ident, attr, standard = records
            if (not ident.volume or not any(ident.identifier) or attr.attributes & 0x410
                    or attr.tag or standard.links != 1 or standard.directory
                    or standard.delete_pending or standard.size <= 0):
                _reject("image_identity_reparse_hardlink_or_state")
            return ident.volume, bytes(ident.identifier), standard.size

        try:
            path = image_name()
            owned[0] = api.open_image(path, IMAGE_ACCESS, 1, None, 3, 0x200000, None)
            image = base._handle(api, owned[0], "CreateFileW.image")
            check()
            first = file_sample(image)
            buf = C.create_unicode_buffer(32768)
            length = base._uint(api.final_path(image, buf, len(buf), 0), 0xffffffff,
                                "GetFinalPathNameByHandleW")
            if not length:
                base._failure(api, "GetFinalPathNameByHandleW")
            check()
            if length >= len(buf) or buf[length] != "\0":
                _reject("image_final_path_length")
            final = buf[:length]
            if not final.startswith("\\\\?\\") or _path(final[4:]).casefold() != path.casefold():
                _reject("image_final_path_mismatch")
            token = HANDLE()
            success = api.open_token(process, TOKEN_QUERY, C.byref(token))
            # Output ownership is established only by successful OpenProcessToken.
            if type(success) is int and -0x80000000 <= success <= 0x7fffffff and success:
                owned[1] = token.value
            base._boolean(api, success, "OpenProcessToken")
            token_handle = base._handle(api, owned[1], "OpenProcessToken.handle")
            check()
            first_token = _token_sample(api, token_handle, check)
            if _token_sample(api, token_handle, check) != first_token:
                _reject("service_token_changed")
            if image_name() != path or file_sample(image) != first:
                _reject("image_changed")
            collected.append((path, (first[0], first[1].hex(), first[2]), *first_token))
        except BaseException as exc:
            primary = exc
            raise
        finally:
            failures = []
            for handle in reversed(owned):
                if type(handle) is int and 0 < handle < base.INVALID_HANDLE - 15:
                    try:
                        base._boolean(api, api.close(handle), "CloseHandle.prerequisite")
                    except BaseException:
                        failures.append(("CloseHandle.prerequisite", "CLOSE_UNCONFIRMED"))
            if failures:
                base._cleanup_uncertain = True
                if isinstance(primary, base.ObservationError):
                    primary.cleanup_failures += tuple(failures)
                elif primary is None:
                    raise base.ObservationError("CLEANUP_UNCONFIRMED", "prerequisite.cleanup",
                                                cleanup_failures=tuple(failures))
                else:
                    primary.add_note("prerequisite cleanup unconfirmed; admission blocked")

    # Load under the base admission slot and deadline, not before it.
    holder = []

    def load():
        api = _calls if _calls is not None else _load_native()
        holder.append(api)
        return api.base

    # An internal loader seam avoids global monkeypatching or loading DLLs on import.
    observation = base._observe_service_process(timeout_ms=timeout_ms, _loader=load,
                                                _inspect=inspect)
    return NativePrerequisites(observation, *collected[0], connection_epoch)
