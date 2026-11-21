"""Offline local development editing. Deliberately NOT wired into Host/MCP.

Cooperative CAS reuses file_edits, not a protected Windows write broker. The
local caller/configuration/interpreter must be trusted. Hostile simultaneous
directory or rename races remain a live-activation blocker.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from threading import Lock
from time import monotonic

from . import file_edits
from .file_reads import ReadBoundaryError


_REPO = Path(__file__).absolute().parents[2]
_MAX_REVIEW = 32768
_REVIEW_SECONDS = 45
_EDIT_LOCK = Lock()  # All local instances share the one pending-edit slot.
_MANUAL_REVIEW = False  # Ambiguous commits latch this process closed.


def _literal(path):
    if type(path) is not str or not path or any(c in path for c in '*?[]\x00'):
        raise ReadBoundaryError('exact local file required')
    p = Path(path)
    if (not p.is_absolute() or str(p) != path or p.drive.startswith('\\\\')
            or not p.is_relative_to(_REPO) or p == _REPO
            or p.relative_to(_REPO).parts[0].startswith('.')
            or any(part in ('.', '..') or part.endswith((' ', '.')) or ':' in part
                   for part in p.parts[1:])):
        raise ReadBoundaryError('literal Next development file required')
    # No private control/configuration paths or Windows device names / ADS.
    if any(part.startswith('.') or re.fullmatch(
            r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?', part)
           for part in p.relative_to(_REPO).parts):
        raise ReadBoundaryError('private or device path forbidden')
    return p


def _chain(p):
    records = []
    for i, part in enumerate((p, *p.parents)):
        info = part.lstat()  # Never resolve away reparse points before checking.
        if (getattr(info, 'st_file_attributes', 0) & 0x400
                or stat.S_ISLNK(info.st_mode)
                or (i == 0 and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))
                or (i > 0 and not stat.S_ISDIR(info.st_mode))):
            raise ReadBoundaryError('linked or abnormal path')
        records.append((str(part), info.st_dev, info.st_ino))
    return tuple(records)


def _scoped_acl_record(path, trusted, *, target):
    import ntsecuritycon
    import win32con
    import win32file
    import win32security

    sections = win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION
    handle = win32file.CreateFile(
        str(path), win32con.READ_CONTROL | ntsecuritycon.FILE_READ_ATTRIBUTES,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
        None, win32con.OPEN_EXISTING,
        win32file.FILE_FLAG_OPEN_REPARSE_POINT | win32con.FILE_FLAG_BACKUP_SEMANTICS, None)
    try:
        if win32file.GetFileInformationByHandle(handle)[0] & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ReadBoundaryError('abnormal or unverifiable ACL')
        descriptor = win32security.GetSecurityInfo(handle, win32security.SE_FILE_OBJECT, sections)
    finally:
        handle.Close()
    if file_edits._sid_string(descriptor.GetSecurityDescriptorOwner()) not in trusted:
        raise ReadBoundaryError('abnormal or unverifiable ACL')

    previous_rank = -1
    for kind, inherited, mask, trustee in file_edits._windows_dacl_entries(descriptor):
        allow = kind == win32security.ACCESS_ALLOWED_ACE_TYPE
        # Explicit deny, explicit allow, inherited deny, inherited allow.
        rank = 2 * inherited + allow
        if (rank < previous_rank or not allow or mask & ~0x1F01FF
                or (target and not inherited) or (trustee not in trusted and mask & 0xD0156)):
            raise ReadBoundaryError('abnormal or unverifiable ACL')
        previous_rank = rank
    if previous_rank == -1:
        raise ReadBoundaryError('abnormal or unverifiable ACL')
    record = win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
        descriptor, win32security.SDDL_REVISION_1, sections)
    if type(record) is not str or not record:
        raise ReadBoundaryError('abnormal or unverifiable ACL')
    return record


def _acl_snapshot(p, sid):
    """Verify the target and every parent and retain their owner and DACL."""
    if os.name != 'nt':
        raise ReadBoundaryError('Windows ACL verification required')
    try:
        import win32api
        import win32con
        import win32security

        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            current = file_edits._sid_string(
                win32security.GetTokenInformation(token, win32security.TokenUser)[0])
        finally:
            token.Close()
        if type(sid) is not str or current != sid:
            raise ReadBoundaryError('abnormal or unverifiable ACL')
        trusted = {current, 'S-1-5-18', 'S-1-5-32-544'}
        return tuple(_scoped_acl_record(part, trusted, target=(index == 0))
                     for index, part in enumerate((p, *p.parents)))
    except Exception as exc:
        raise ReadBoundaryError('abnormal or unverifiable ACL') from exc


@dataclass(frozen=True)
class _Snapshot:
    data: bytes
    chain: tuple
    acl: tuple
    version: tuple


def _inspect(p, sid):
    chain = _chain(p)
    path_info = p.lstat()
    acl = _acl_snapshot(p, sid)
    with p.open('rb') as source:
        info = os.fstat(source.fileno())
        data = source.read(65537)
        after = os.fstat(source.fileno())
    version = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if (len(data) > 65536 or info.st_nlink != 1 or after.st_nlink != 1
            or not stat.S_ISREG(info.st_mode)
            or (info.st_dev, info.st_ino) != chain[0][1:]
            or version(info) != version(after)
            # Windows Python 3.13 fstat ctime can be change time while lstat
            # ctime is birth time. Compare each API's ctime to itself, retaining
            # change detection without equating different timestamp meanings.
            or version(path_info) != version(p.lstat())
            or version(after)[:4] != version(path_info)[:4]
            or chain != _chain(p) or acl != _acl_snapshot(p, sid)):
        raise ReadBoundaryError('path, content or ACL changed while reading')
    data.decode('utf-8')
    return _Snapshot(data, chain, acl, version(after))


@dataclass(frozen=True)
class ScopedDevelopmentEditPolicy:
    """Only trusted local code may construct this immutable exact-file list.

    Empty by default. No roots/profile, environment loader, setter, glob, create
    operation or MCP registration. Configuration changes need a new instance.
    """
    files: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.files) is not tuple or len(set(self.files)) != len(self.files):
            raise ReadBoundaryError('immutable exact-file tuple required')
        for path in self.files:
            _chain(_literal(path))

    def target(self, path):
        p = _literal(path)
        if path not in self.files:
            raise ReadBoundaryError('file not locally allowlisted')
        _chain(p)
        return p


def _owner_guard():
    from .windows_owner import OwnerSessionGuard
    return OwnerSessionGuard()


class ScopedDevelopmentEditAdapter:
    """Synchronous local API. Each call needs a fresh real Owner GUI click.

    There is no public approve/commit API and no cached grant to replay. A busy
    call fails immediately, including calls from a second adapter instance.
    """
    def __init__(self, policy):
        if type(policy) is not ScopedDevelopmentEditPolicy:
            raise TypeError('explicit local scoped development policy required')
        self._policy = policy

    def request(self, path, content, expected_sha256):
        global _MANUAL_REVIEW
        if not _EDIT_LOCK.acquire(blocking=False):
            return {'status': 'BUSY'}
        attempted = False
        writer_returned = False
        try:
            if _MANUAL_REVIEW:
                return self._unknown()
            if (type(content) is not str or type(expected_sha256) is not str
                    or re.fullmatch('[0-9a-f]{64}', expected_sha256) is None):
                raise ReadBoundaryError('text and prior SHA-256 required')
            data = content.encode('utf-8')
            if len(data) > 65536:
                raise ReadBoundaryError('proposal too large')
            p = self._policy.target(path)
            guard = _owner_guard()
            guard.verify()
            before = _inspect(p, guard.sid)
            if sha256(before.data).hexdigest() != expected_sha256:
                raise file_edits.StalePreviewError('stale proposal')
            new_sha = sha256(data).hexdigest()
            preview = file_edits.preview_text_edit(path, (str(p.parent),), content, expected_sha256)
            if (preview['diff_truncated'] or preview['path'] != path
                    or preview['old_sha256'] != expected_sha256
                    or preview['new_sha256'] != new_sha
                    or preview['changed'] != (before.data != data)):
                raise ReadBoundaryError('preview does not bind to proposal')
            # JSON escaping exposes control characters, CRLF and missing EOF LF.
            review = json.dumps({'operation': 'replace ONE existing development file',
                'path': path, 'old_sha256': expected_sha256, 'new_sha256': new_sha,
                'before_utf8': before.data.decode('utf-8'), 'after_utf8': content,
                'diff': preview['diff']}, ensure_ascii=True, indent=2)
            if len(review) > _MAX_REVIEW:
                raise ReadBoundaryError('complete review exceeds UI limit')
            if before.data == data:
                return {'status': 'UNCHANGED_LOCAL_DEV', 'sha256': expected_sha256}
            deadline = monotonic() + _REVIEW_SECONDS
            if file_edits._show_edit_review(preview, complete_review=review, deadline=deadline) is not True:
                return {'status': 'DENIED'}

            def revalidate():
                guard.verify()
                if (self._policy.target(path) != p or _inspect(p, guard.sid) != before):
                    raise ReadBoundaryError('reviewed target changed')
                if monotonic() >= deadline:
                    raise TimeoutError('approval expired')

            revalidate()
            staged_identity = None

            def precommit(target, stage):
                nonlocal attempted, staged_identity
                revalidate()
                staged = _inspect(stage, guard.sid)
                if (target != p or stage.parent != p.parent or staged.data != data
                        or sha256(staged.data).hexdigest() != new_sha
                        or staged.acl != before.acl):
                    raise ReadBoundaryError('staged proposal or ACL mismatch')
                staged_identity = staged.chain[0][1:]
                # Recheck after the slow ACL gate, immediately before replace.
                revalidate()
                attempted = True

            result = file_edits.replace_text(path, (str(p.parent),), content,
                                             expected_sha256, _before_commit=precommit)
            writer_returned = True
            guard.verify()
            after = _inspect(self._policy.target(path), guard.sid)
            if (result.get('status') != 'REPLACED_LOCAL_DEV' or not attempted
                    or result.get('sha256') != new_sha or sha256(after.data).hexdigest() != new_sha
                    or after.data != data or after.acl != before.acl
                    or after.chain[0][1:] != staged_identity or after.chain[1:] != before.chain[1:]):
                raise ReadBoundaryError('post-write verification failed')
            return {'status': 'REPLACED_LOCAL_DEV', 'sha256': new_sha}
        except BaseException as exc:
            if attempted or writer_returned:
                _MANUAL_REVIEW = True
                return self._unknown()
            if not isinstance(exc, Exception):
                raise  # No commit attempted; respect local cancellation.
            if isinstance(exc, TimeoutError):
                return {'status': 'DENIED', 'error': 'APPROVAL_EXPIRED'}
            return {'status': 'DENIED', 'error': 'SCOPED_EDIT_REJECTED'}
        finally:
            _EDIT_LOCK.release()

    @staticmethod
    def _unknown():
        return {'status': 'UNKNOWN', 'recovery': 'MANUAL_REVIEW_REQUIRED',
                'auto_rollback': False}
