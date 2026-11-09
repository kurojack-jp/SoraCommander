"""Offline signed BOM/artifact cross-check; caller-key PREVIEW, always HOLD_LIVE.

No installation, execution, extraction, network or protected-root inspection.
Windows chain policy and an arbitrary supplied key do not establish publisher
provenance. Namespace checks are observations, not a hostile concurrent writer
or protected ACL guarantee. Scanners retain their original limits and policy.
"""
from contextlib import ExitStack, contextmanager
import ctypes
from dataclasses import dataclass, field
import ntpath
import os
from pathlib import Path
import re
import stat

from .release_broker_bom_receipt import verify_signed_receipt, RESERVED_NAMES
from .release_layout import layout_specification
from .release_preflight import inspect_offline_candidate
from .release_wheel_payload_audit import audit_wheel_payload
from .release_windows_signature_audit import inspect_embedded_signature

MAX_ENTRIES = 64
MAX_NATIVE_DEPTH = 8


class BrokerBomFilesAuditError(ValueError):
    """Incomplete artifact evidence; no authorization can be inferred."""


@dataclass(frozen=True, slots=True)
class BrokerArtifactsEvidence:
    publisher: str
    sequence: int
    verified_public_key_fingerprint: str
    candidate_manifest_sha256: str
    scanned_wheel_names: tuple[str, ...]
    scanned_native_paths: tuple[str, ...]
    native_roles: tuple[tuple[str, str], ...]
    wheel_count: int
    native_binary_count: int
    candidate_content_verified: bool = field(default=True, init=False)
    wheel_payloads_verified: bool = field(default=True, init=False)
    native_signatures_observed: bool = field(default=True, init=False)
    external_trust_anchor_origin_verified: bool = field(default=False, init=False)
    publisher_pinned: bool = field(default=False, init=False)
    deployment_authorized: bool = field(default=False, init=False)
    install_permitted: bool = field(default=False, init=False)
    launch_permitted: bool = field(default=False, init=False)
    remote_authorized: bool = field(default=False, init=False)
    status: str = field(default="HOLD_LIVE", init=False)


def _path(value):
    if not isinstance(value, (str, Path)):
        raise BrokerBomFilesAuditError("explicit absolute staging path required")
    raw = str(value)
    if (not 4 <= len(raw) <= 259 or re.match(r"^[A-Za-z]:\\", raw) is None
            or any(ord(c) < 32 for c in raw) or any(c in raw[2:] for c in '/:*?"<>|')):
        raise BrokerBomFilesAuditError("ordinary absolute local path required")
    for part in raw[3:].split("\\"):
        if (not part or part in (".", "..") or part.endswith((" ", "."))
                or part.split(".", 1)[0].casefold() in RESERVED_NAMES
                or re.fullmatch(r"(?:com|lpt)[¹²³](?:\..*)?", part.casefold())):
            raise BrokerBomFilesAuditError("ambiguous, traversal or device path denied")
    return Path(raw)


@contextmanager
def _directory(path):
    """Hold ordinary ancestor directories; reject drive/mount/SUBST aliases.

    Final DOS paths must equal the literal path. No resolution through a link
    is accepted. Handles deny delete sharing until the aggregate scan ends.
    """
    if os.name != "nt":
        raise BrokerBomFilesAuditError("Windows fixed-drive staging required")
    kernel = ctypes.WinDLL("kernel32.dll", winmode=0x800)
    declarations = (
        ("GetDriveTypeW", [ctypes.c_wchar_p], ctypes.c_uint),
        ("CreateFileW", [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint,
                         ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p], ctypes.c_void_p),
        ("GetFinalPathNameByHandleW", [ctypes.c_void_p, ctypes.c_wchar_p,
                                      ctypes.c_uint, ctypes.c_uint], ctypes.c_uint),
        ("GetFileInformationByHandleEx", [ctypes.c_void_p, ctypes.c_int,
                                         ctypes.c_void_p, ctypes.c_uint], ctypes.c_int),
        ("CloseHandle", [ctypes.c_void_p], ctypes.c_int),
    )
    for name, args, result in declarations:
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    handles = []
    try:
        if kernel.GetDriveTypeW(path.anchor) != 3:
            raise BrokerBomFilesAuditError("local fixed drive required")
        for ancestor in (*reversed(path.parents), path):
            info = ancestor.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise BrokerBomFilesAuditError("ordinary nonreparse directory required")
            handle = kernel.CreateFileW(str(ancestor), 0x80, 3, None, 3,
                                        0x00200000 | 0x02000000, None)
            if handle in (None, 0, ctypes.c_void_p(-1).value):
                raise BrokerBomFilesAuditError("directory handle unavailable")
            handles.append(handle)
            attributes = (ctypes.c_uint * 2)()
            if (not kernel.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes))
                    or attributes[0] & 0x400 or not attributes[0] & 0x10):
                raise BrokerBomFilesAuditError("opened directory is reparse or nonordinary")
            final = ctypes.create_unicode_buffer(32768)
            count = kernel.GetFinalPathNameByHandleW(handle, final, len(final), 0)
            if (not 0 < count < len(final) or not final.value.startswith("\\\\?\\")
                    or final.value[4:].rstrip("\\").casefold()
                    != str(ancestor).rstrip("\\").casefold()):
                raise BrokerBomFilesAuditError("mount alias or redirected directory denied")
        yield
    finally:
        failed = False
        for handle in reversed(handles):
            if not kernel.CloseHandle(handle):
                failed = True
        if failed:
            raise BrokerBomFilesAuditError("directory handle CLOSE failed")


def _inventory(root, *, native, stack):
    files, directories, identities, names = [], [], [], set()
    total = 0

    def walk(directory, prefix, depth):
        nonlocal total
        with os.scandir(directory) as entries:
            for entry in entries:
                total += 1
                if total > MAX_ENTRIES or depth > MAX_NATIVE_DEPTH:
                    raise BrokerBomFilesAuditError("artifact entry/depth bound exceeded")
                relative = prefix + entry.name
                folded = relative.casefold()
                if folded in names:
                    raise BrokerBomFilesAuditError("casefold collision in artifact tree")
                names.add(folded)
                path = _path(root / relative.replace("/", "\\"))
                # DirEntry.stat() can report an unknown Windows file ID/link
                # count. Read Path.lstat() once for reparse and hardlink checks.
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise BrokerBomFilesAuditError("artifact reparse/link denied")
                identities.append((relative, info.st_dev, info.st_ino, info.st_size,
                                   info.st_mtime_ns, info.st_nlink))
                if stat.S_ISDIR(info.st_mode) and native:
                    directories.append(relative)
                    stack.enter_context(_directory(path))
                    walk(path, relative + "/", depth + 1)
                elif (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                      and path.suffix.casefold() in ((".exe", ".dll", ".pyd") if native else (".whl",))):
                    files.append(relative)
                else:
                    raise BrokerBomFilesAuditError("unexpected entry or nonordinary single-link artifact")

    walk(root, "", 1)
    return (tuple(sorted(files, key=str.casefold)),
            tuple(sorted(directories, key=str.casefold)), tuple(sorted(identities)))


def audit_signed_broker_artifacts(*, candidate_root, wheelhouse_root, native_root,
                                  receipt_bytes, trusted_ed25519_public_key,
                                  trusted_publisher, minimum_sequence, layout,
                                  installed_sequence=None):
    """Verify the v2 signature, candidate, EVERY wheel and EVERY native binary.

    All pins come exclusively from signature verification under exact caller
    policy. No verified-evidence or scanner-callback substitution API exists.
    Errors propagate; no fallback trust, omissions or count-only acceptance.
    """
    verified = verify_signed_receipt(
        receipt_bytes, trusted_ed25519_public_key=trusted_ed25519_public_key,
        trusted_publisher=trusted_publisher, minimum_sequence=minimum_sequence,
        installed_sequence=installed_sequence)
    layout_specification(layout)
    roots = tuple(_path(p) for p in (candidate_root, wheelhouse_root, native_root))
    protected = ntpath.normcase(layout.code_root).rstrip("\\")
    for root in roots:
        normalized = ntpath.normcase(str(root)).rstrip("\\")
        if (normalized == protected or normalized.startswith(protected + "\\")
                or protected.startswith(normalized + "\\")):
            raise BrokerBomFilesAuditError("deployed code root overlap denied before inspection")
    candidate, wheels, native = roots
    with ExitStack() as stack:
        stack.enter_context(_directory(candidate))
        content = inspect_offline_candidate(
            candidate, expected_manifest_sha256=verified.candidate_manifest_sha256, layout=layout)
        if content.content_verified is not True:
            raise BrokerBomFilesAuditError("candidate scanner did not verify content")
        for root in (wheels, native):
            stack.enter_context(_directory(root))
        wheel_inventory = _inventory(wheels, native=False, stack=stack)
        native_inventory = _inventory(native, native=True, stack=stack)
        wheel_names = tuple(pin.filename for pin in verified.wheels)
        native_paths = tuple(pin.relative_path for pin in verified.native_binaries)
        required_dirs = {p[:i] for p in native_paths for i, c in enumerate(p) if c == "/"}
        if (wheel_inventory[0] != wheel_names or native_inventory[0] != native_paths
                or set(native_inventory[1]) != required_dirs):
            raise BrokerBomFilesAuditError("artifact names/directories differ from signed BOM")
        for pin in verified.wheels:
            result = audit_wheel_payload(wheels / pin.filename,
                                        expected_sha256=pin.sha256, expected_size=pin.bytes)
            if (result.archive_sha256 != pin.sha256 or result.archive_size != pin.bytes
                    or result.record_coverage_verdict != "PASS"):
                raise BrokerBomFilesAuditError("wheel scanner evidence differs from signed pin/RECORD")
        for pin in verified.native_binaries:
            result = inspect_embedded_signature(
                native / pin.relative_path, expected_sha256=pin.sha256, expected_size=pin.bytes)
            if (result.sha256 != pin.sha256 or result.size != pin.bytes
                    or result.windows_chain_policy_accepted is not True
                    or result.winverifytrust_status != 0 or result.state_close_status != 0):
                raise BrokerBomFilesAuditError("native scanner evidence differs from signed pin/trust")
        # Detect late additions/removals/replacements, including already scanned files.
        if (wheel_inventory != _inventory(wheels, native=False, stack=stack)
                or native_inventory != _inventory(native, native=True, stack=stack)):
            raise BrokerBomFilesAuditError("artifact tree changed during scan")
    return BrokerArtifactsEvidence(
        verified.publisher, verified.sequence, verified.verified_public_key_fingerprint,
        verified.candidate_manifest_sha256, wheel_names, native_paths,
        tuple((pin.relative_path, pin.role) for pin in verified.native_binaries),
        len(wheel_names), len(native_paths))
