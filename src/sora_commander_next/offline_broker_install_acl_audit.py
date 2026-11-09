"""DEV-only, read-only Windows code-root ACL audit.

This checks only the existing SoraCommanderNext foundation paths. It never
creates folders, edits ACLs, requests elevation, imports installed code, starts
a Broker, or authorizes release/remote use. Directory-level ACL evidence is
NOT executable image provenance, a signed release, or verified descendants.
"""
from dataclasses import dataclass
import os
from pathlib import Path

from .offline_machine_install_plan import observe_machine_known_folders_readonly
from .release_layout import _overlap, _path

SYSTEM = "S-1-5-18"
ADMINS = "S-1-5-32-544"
USERS = "S-1-5-32-545"
FULL = 0x1F01FF
READ_EXEC = 0x1200A9


@dataclass(frozen=True, slots=True)
class DirectoryAclObservation:
    label: str
    state: str
    checked: bool
    code_root_acl_match: bool = False
    protected_release_verified: bool = False
    launch_authorized: bool = False


def classify_code_root_acl(*, owner_sid, protected, entries):
    """Pure exact ACL comparison; handles only extracted read-only values.

    Entries are tuples (sid, allow, inherited, flags, mask), never client
    supplied grants. Missing, extra, inherited, deny or writable Users ACE
    all fail-closed. Cannot establish parent-chain or content integrity.
    """
    if (type(owner_sid) is not str or owner_sid != ADMINS
            or protected is not True or type(entries) is not tuple
            or len(entries) != 3):
        return False
    expected = {SYSTEM: FULL, ADMINS: FULL, USERS: READ_EXEC}
    seen = set()
    for item in entries:
        if type(item) is not tuple or len(item) != 5:
            return False
        sid, allowed, inherited, flags, rights = item
        if (sid not in expected or sid in seen or allowed is not True
                or inherited is not False or flags != 3
                or type(rights) is not int or rights != expected[sid]):
            return False
        seen.add(sid)
    return seen == set(expected)


def _read_acl(path):
    import win32security
    # Do not chase links at the target. Ancestor links require a separate
    # verified installation review; this checks only an existing foundation.
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise PermissionError("linked install directory")
    if not path.is_dir():
        raise FileNotFoundError("foundation directory unavailable")
    try:
        sd = win32security.GetFileSecurity(
            str(path),
            win32security.DACL_SECURITY_INFORMATION
            | win32security.OWNER_SECURITY_INFORMATION)
    except Exception as exc:
        # pywintypes.error does not necessarily subclass OSError or
        # PermissionError. Inability to read security metadata is UNKNOWN,
        # never a successful ACL audit or a reason to elevate.
        raise PermissionError("security descriptor unavailable") from exc
    owner_sid = win32security.ConvertSidToStringSid(
        sd.GetSecurityDescriptorOwner())
    control, _ = sd.GetSecurityDescriptorControl()
    protected = bool(control & win32security.SE_DACL_PROTECTED)
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None or dacl.GetAceCount() > 32:
        raise PermissionError("empty or unbounded DACL")
    entries = []
    for i in range(dacl.GetAceCount()):
        (kind, flags), mask, sid = dacl.GetAce(i)[:3]
        entries.append((
            win32security.ConvertSidToStringSid(sid),
            kind == win32security.ACCESS_ALLOWED_ACE_TYPE,
            bool(flags & win32security.INHERITED_ACE),
            flags & (win32security.CONTAINER_INHERIT_ACE
                     | win32security.OBJECT_INHERIT_ACE),
            mask,
        ))
        # Unexpected ACE flag bits (e.g., inheritance control or object
        # ACE details) fail rather than being silently ignored.
        if flags & ~(win32security.CONTAINER_INHERIT_ACE
                     | win32security.OBJECT_INHERIT_ACE):
            raise PermissionError("unexpected ACE flags")
    return owner_sid, protected, tuple(entries)


def _foundation_paths_readonly():
    """Observe local OS Known Folders, never externally supplied MCP paths."""
    observed = observe_machine_known_folders_readonly()
    program_files = _path(observed["program_files"])
    program_data = _path(observed["program_data"])
    if _overlap(program_files, program_data):
        raise ValueError("Windows program/data folders overlap")
    code = Path(program_files) / "SoraCommanderNext"
    admin_data = Path(program_data) / "SoraCommanderNext"
    return (("CODE_ROOT", code), ("ADMIN_DATA", admin_data),
            ("TRUST", admin_data / "Trust"))


def audit_foundation_readonly():
    """Read existing local Known Folder foundations; never authorize install."""
    if os.name != "nt":
        raise RuntimeError("Windows-only foundation audit")
    try:
        paths = _foundation_paths_readonly()
    except (KeyError, TypeError, ValueError, RuntimeError, OSError):
        # Missing or invalid OS observations are UNKNOWN, not trusted defaults.
        return tuple(DirectoryAclObservation(label, "ACCESS_OR_PATH_UNAVAILABLE",
                                             checked=False)
                     for label in ("CODE_ROOT", "ADMIN_DATA", "TRUST"))
    results = []
    for label, path in paths:
        try:
            owner_sid, protected, entries = _read_acl(path)
            match = (classify_code_root_acl(
                owner_sid=owner_sid, protected=protected, entries=entries)
                     if label == "CODE_ROOT" else False)
            # Data/Trust ACL policy is intentionally not invented; merely
            # reading them here cannot attest an unknown expected policy.
            state = ("DIRECTORY_DACL_MATCH_ONLY" if match
                     else "ACL_OBSERVED_NOT_ACCEPTED")
            results.append(DirectoryAclObservation(
                label, state, checked=True, code_root_acl_match=match))
        except (OSError, PermissionError, FileNotFoundError, ImportError):
            results.append(DirectoryAclObservation(
                label, "ACCESS_OR_PATH_UNAVAILABLE", checked=False))
    return tuple(results)
