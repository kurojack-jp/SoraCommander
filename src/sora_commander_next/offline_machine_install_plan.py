"""OFFLINE Windows multi-PC installation layout PREVIEW; no install/authorization.

Trusted installer must resolve the actual Windows Known Folder IDs and verify
the caller's token, code signature, architecture, ACL, reparse chain and release
sequence separately. This module does not elevate, write, import candidate
code, copy secrets, register a device or run Broker/MCP.
"""
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import ntpath
import os
import platform
from uuid import UUID

from .release_layout import _path, _overlap, review_release_layout

PRODUCT = "SoraCommanderNext"

# Stable Windows KNOWNFOLDERID values, NOT environment-variable names.
FOLDERS = {
    "program_files": "6D809377-6AF0-444B-8957-A3773F02200E",  # x64
    "program_data": "62AB5D82-FDC1-4DC3-A9DD-070D1D495D97",
    "local_app_data": "F1B32785-6FBA-4FCF-9D55-7B8E7F157091",
}


class MachineLayoutRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OfflineMachineInstallPlan:
    code_root: str
    admin_data_root: str
    trust_root: str
    owner_data_root: str
    staging_root: str
    architecture: str
    supported_architecture: bool
    status: str
    install_authorized: bool = False
    broker_launch_authorized: bool = False
    remote_authorized: bool = False
    existing_owner_grants_importable: bool = False
    device_registration_complete: bool = False


def preview_machine_install(*, program_files, program_data, local_app_data,
                            staging_root, architecture):
    """Build paths from explicit OS-folder observations for an offline preview.

    Inputs are NOT independently authenticated and MUST NOT be MCP parameters.
    Standard users must never choose arbitrary writable code/trust roots.
    The fixed release application subdirectory is deliberately not
    configurable by an external caller.
    """
    if type(architecture) is not str or architecture.upper() not in {
            "AMD64", "X86_64", "ARM64", "AARCH64", "X86", "I386"}:
        raise MachineLayoutRejected("unknown Windows architecture")
    try:
        pf, pd, lad, stage = (_path(item) for item in (
            program_files, program_data, local_app_data, staging_root))
        code = ntpath.join(pf, PRODUCT).casefold()
        admin = ntpath.join(pd, PRODUCT).casefold()
        trust = ntpath.join(admin, "Trust").casefold()
        owner = ntpath.join(lad, PRODUCT).casefold()
        # Every top-level role is disjoint, including download/untrusted stage.
        review_release_layout(development_root=stage, code_root=code,
                              data_root=owner)
        review_release_layout(development_root=admin, code_root=code,
                              data_root=owner)
        review_release_layout(development_root=stage, code_root=admin,
                              data_root=owner)
        if (_overlap(pf, pd) or _overlap(pf, lad) or _overlap(pd, lad)
                or _overlap(stage, admin) or _overlap(stage, code)
                or _overlap(stage, owner)):
            raise MachineLayoutRejected("Windows folder roles overlap")
    except ValueError as exc:
        raise MachineLayoutRejected("invalid or overlapping Windows layout") from exc

    # x64 only is currently acceptance-tested; ARM64 / 32-bit need separate
    # wheelhouse, image, Windows Job, pywin32 and signed-installer acceptance.
    x64 = architecture.upper() in {"AMD64", "X86_64"}
    return OfflineMachineInstallPlan(
        code_root=code, admin_data_root=admin, trust_root=trust,
        owner_data_root=owner, staging_root=stage,
        architecture=architecture.upper(), supported_architecture=x64,
        status=("MULTI_PC_LAYOUT_PREVIEW_HOLD" if x64
                else "UNTESTED_ARCHITECTURE_HOLD"),
    )


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _known_folder(guid_text):
    from ctypes import windll
    shell = windll.shell32
    ole = windll.ole32
    fn = shell.SHGetKnownFolderPath
    fn.argtypes = (ctypes.POINTER(_GUID), wintypes.DWORD,
                   wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p))
    fn.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    ole.CoTaskMemFree.restype = None
    guid = _GUID.from_buffer_copy(UUID(guid_text).bytes_le)
    pointer = ctypes.c_void_p()
    hr = fn(ctypes.byref(guid), 0, None, ctypes.byref(pointer))
    if hr != 0 or not pointer.value:
        if pointer.value:
            ole.CoTaskMemFree(pointer)
        raise MachineLayoutRejected("Windows known folder unavailable")
    try:
        return ctypes.wstring_at(pointer.value)
    finally:
        ole.CoTaskMemFree(pointer)


def observe_machine_known_folders_readonly():
    """Actual local Windows observations, with NO trust or install decision.

    x64 Program Files is intentionally requested (not Program Files x86).
    The staging/download path must be selected separately by the local
    installer; this function accepts no remote input and returns no secrets.
    """
    if os.name != "nt":
        raise MachineLayoutRejected("Windows-only known folder observation")
    roots = {key: _known_folder(guid) for key, guid in FOLDERS.items()}
    # Caller must independently check that the current OS user is the intended
    # Owner; Known Folder API path observations are not SID/LUID proof.
    return {**roots, "architecture": platform.machine().upper()}
