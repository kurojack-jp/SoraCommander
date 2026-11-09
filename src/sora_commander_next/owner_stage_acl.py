"""Windows Owner-local staged FILE DACL protection before atomic replace.

This only changes a newly created, caller-owned STAGING file, never its
parent or a pre-existing production target. It does NOT prove Owner identity
or allow a remote MCP request to select any path. Protected Owner storage
and local session MUST have been independently verified by the caller.

Uses pywin32 Win32 ACL APIs; absent/unavailable APIs fail closed.
"""
import os
from pathlib import Path
import stat


class OwnerStageAclError(PermissionError):
    pass


def _sids():
    import win32api
    import win32security
    import win32con
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        owner = win32security.GetTokenInformation(
            token, win32security.TokenUser)[0]
    finally:
        token.Close()
    system = win32security.ConvertStringSidToSid("S-1-5-18")
    administrators = win32security.ConvertStringSidToSid("S-1-5-32-544")
    identities = (owner, system, administrators)
    if len({win32security.ConvertSidToStringSid(s) for s in identities}) != 3:
        raise OwnerStageAclError("service or duplicate Owner SID")
    return identities


def _check(file_path, allowed):
    import ntsecuritycon
    import win32security
    descriptor = win32security.GetFileSecurity(
        str(file_path), win32security.DACL_SECURITY_INFORMATION)
    control, _ = descriptor.GetSecurityDescriptorControl()
    if not (control & win32security.SE_DACL_PROTECTED):
        raise OwnerStageAclError("staged file ACL inheritance not disabled")
    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None or dacl.GetAceCount() != 3:
        raise OwnerStageAclError("staged file DACL differs from 3 expected ACEs")
    seen = set()
    for index in range(3):
        ace = dacl.GetAce(index)
        (type_id, flags), mask, sid = ace[:3]
        sid_string = win32security.ConvertSidToStringSid(sid)
        if (type_id != win32security.ACCESS_ALLOWED_ACE_TYPE
                or flags & win32security.INHERITED_ACE
                or mask & ntsecuritycon.FILE_ALL_ACCESS
                != ntsecuritycon.FILE_ALL_ACCESS
                or sid_string not in allowed
                or sid_string in seen):
            raise OwnerStageAclError("untrusted file ACL entry")
        seen.add(sid_string)
    if seen != allowed:
        raise OwnerStageAclError("staged file ACL identity mismatch")
    return True


def protect_owner_stage_local(stage_path, *, fresh_lock=False):
    """Explicitly protect a FRESH stage only; never apply to an existing target.

    The caller passes the newly created random NamedTemporaryFile path, with
    the handle CLOSED and bytes flushed/fsynced. No remote-controlled paths.
    """
    if os.name != "nt":
        raise OwnerStageAclError("protected Owner stage requires Windows")
    import ntsecuritycon
    import win32security
    p = Path(stage_path)
    if (not p.is_absolute() or p.is_symlink() or p.is_junction()
            or not p.is_file()):
        raise OwnerStageAclError("invalid Owner staged file")
    info = p.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OwnerStageAclError("unsafe Owner staged file")
    if fresh_lock:
        if p.name != "owner-session-journal.json.lock":
            raise OwnerStageAclError("invalid Owner journal lock name")
    elif (not p.name.startswith((".sora-result-", ".sora-session-"))
          or p.suffix != ".tmp"):
        raise OwnerStageAclError("invalid Owner stage filename")
    identities = _sids()
    allowed = {win32security.ConvertSidToStringSid(s) for s in identities}
    descriptor = win32security.SECURITY_DESCRIPTOR()
    dacl = win32security.ACL()
    for sid in identities:
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS, 0,
            ntsecuritycon.FILE_ALL_ACCESS, sid)
    descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
    descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED)
    try:
        win32security.SetFileSecurity(
            str(p),
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            descriptor)
        _check(p, allowed)
    except Exception as exc:
        raise OwnerStageAclError("unable to protect Owner staged output") from exc
    fresh = p.stat()
    if (fresh.st_dev, fresh.st_ino) != (info.st_dev, info.st_ino):
        raise OwnerStageAclError("Owner stage changed during ACL preparation")
    return True


def verify_owner_stage_after_replace(path):
    """Verify final file DACL after atomic replacement, never alter it."""
    if os.name != "nt":
        raise OwnerStageAclError("protected Owner verification requires Windows")
    p = Path(path)
    if (not p.is_file() or p.is_symlink() or p.is_junction()
            or p.stat().st_nlink != 1):
        raise OwnerStageAclError("invalid Owner result target")
    import win32security
    allowed = {win32security.ConvertSidToStringSid(s) for s in _sids()}
    return _check(p, allowed)
