"""Bounded local text CAS draft. NOT safe for remote activation or hostile writers."""
from hashlib import sha256
from .file_review_outcome import _VerifiedFileReviewExpiry
from dataclasses import dataclass
from math import ceil, isfinite
import difflib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unicodedata
from time import monotonic
from .file_reads import _check, _read_inspection_text, ReadBoundaryError


def escape_review_display(value):
    """Show invisible Unicode controls visibly without changing review bytes.

    Newlines remain structural; everything else with Unicode category Cc/Cf
    is escaped, including bidi controls, zero-width marks, and terminal ESC.
    """
    if type(value) is not str:
        raise ValueError("invalid Owner review display text")
    return "".join(
        ("\\u%04X" % ord(char) if ord(char) <= 0xFFFF
         else "\\U%08X" % ord(char))
        if unicodedata.category(char) in ("Cc", "Cf") and char not in "\r\n"
        else char
        for char in value
    )


class StalePreviewError(ReadBoundaryError):
    """Expected file SHA-256 no longer matches; preview must be refreshed."""


class WriteNotStartedError(ReadBoundaryError):
    """Verified pre-replace rejection; this invocation did not attempt a write."""


class CreateVerificationError(ReadBoundaryError):
    """Creation started, but its result requires manual reconciliation."""


_CREATE_ROOT = Path(__file__).absolute().parents[2] / 'tests' / 'real_mcp'
_CREATE_REVIEW_LIMIT = 32768


def _create_target(path, roots, *, existing=False):
    """Literal disposable path only; reject every reparse ancestor as well."""
    if type(path) is not str or tuple(roots) != (str(_CREATE_ROOT),):
        raise ReadBoundaryError('disposable create root required')
    p = Path(path)
    if (not p.is_absolute() or p.parent != _CREATE_ROOT
            or not re.fullmatch(r'owner-edit-pilot-[A-Za-z0-9_-]+\.txt', p.name)):
        raise ReadBoundaryError('disposable create target required')
    for ancestor in (_CREATE_ROOT, *_CREATE_ROOT.parents):
        info = ancestor.lstat()
        if (not stat.S_ISDIR(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ReadBoundaryError('linked create ancestor')
    try:
        info = p.lstat()
    except FileNotFoundError:
        if existing:
            raise ReadBoundaryError('created file missing') from None
    else:
        if (not existing or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ReadBoundaryError('existing or linked create target')
    return p


def preview_text_create(path, roots, content):
    """Complete escaped text review; no absent-file SHA masquerading as an edit."""
    if type(content) is not str:
        raise ReadBoundaryError('UTF-8 text required')
    try:
        data = content.encode('utf-8')
    except UnicodeError as exc:
        raise ReadBoundaryError('UTF-8 text required') from exc
    if len(data) > 65536:
        raise ReadBoundaryError('invalid write size')
    p = _create_target(path, roots)
    # Escapes expose NUL, control characters, and exact line endings in the UI.
    review = json.dumps(content, ensure_ascii=True)
    return {'path': str(p), 'bytes': len(data), 'new_sha256': sha256(data).hexdigest(),
            'review': review[:_CREATE_REVIEW_LIMIT],
            'review_truncated': len(review) > _CREATE_REVIEW_LIMIT}


def _verify_created_text(path, roots, expected_sha256, identity=None):
    p = _create_target(path, roots, existing=True)
    with p.open('rb') as source:
        info = os.fstat(source.fileno())
        data = source.read(65537)
    again = _create_target(path, roots, existing=True).lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (again.st_dev, again.st_ino)
            or (identity is not None and identity != (info.st_dev, info.st_ino))
            or len(data) > 65536 or sha256(data).hexdigest() != expected_sha256):
        raise CreateVerificationError('created file verification failed')
    return sha256(data).hexdigest()


def create_text_exclusive(path, roots, content, expected_sha256, *, _before_create=None):
    """Local cooperative pilot only. Never overwrite or automatically roll back."""
    preview = preview_text_create(path, roots, content)
    if preview['review_truncated'] or preview['new_sha256'] != expected_sha256:
        raise ReadBoundaryError('incomplete or changed create proposal')
    p = _create_target(path, roots)
    # Optional stricter local adapter revalidation; never supplied over MCP.
    if _before_create is not None:
        _before_create(p)
    # x+b maps to exclusive creation; even a dangling link must fail here.
    with p.open('x+b') as out:
        try:
            info = os.fstat(out.fileno())
            identity = (info.st_dev, info.st_ino)
            current = _create_target(path, roots, existing=True).lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or identity != (current.st_dev, current.st_ino)):
                raise CreateVerificationError('created path identity changed')
            data = content.encode('utf-8')
            if out.write(data) != len(data):
                raise CreateVerificationError('incomplete create write')
            out.flush()
            os.fsync(out.fileno())
            digest = _verify_created_text(path, roots, expected_sha256, identity)
        except Exception as exc:
            raise CreateVerificationError('create requires manual review') from exc
    return {'status': 'CREATED_LOCAL_DEV', 'bytes': len(data), 'sha256': digest}


@dataclass(frozen=True)
class _DevFileReviewOutcome:
    """In-process UI observation only; never a write grant or Host status.

    EXPIRED requires the actual timer callback at/after the deadline. A late
    click or an unexplained UI exit is UNAVAILABLE, not proof of timer expiry.
    """
    decision: str
    reason: str
    deadline: float | None = None
    timer_at: float | None = None
    review_token: object = None

    def __bool__(self):
        raise TypeError('DEV review observations are not approval booleans')


class _FileReviewDecision:
    """Single Tk event-loop latch. Final validation may revoke approval only."""
    def __init__(self, root, deadline, verify, review_token=None):
        self.root, self.deadline, self.verify = root, deadline, verify
        self.review_token = review_token
        self.result = None

    def decide(self, decision):
        if self.result is not None:
            return
        reason = 'LOCAL_EVENT'
        if decision == 'APPROVED':
            try:
                if self.verify is not None:
                    self.verify()
            except Exception:
                decision, reason = 'UNAVAILABLE', 'SESSION_GUARD'
            else:
                if monotonic() >= self.deadline:
                    decision, reason = 'UNAVAILABLE', 'LATE_APPROVAL'
        elif decision == 'EXPIRED' and monotonic() < self.deadline:
            decision, reason = 'UNAVAILABLE', 'EARLY_TIMER'
        self.result = _DevFileReviewOutcome(
            decision, reason, self.deadline,
            monotonic() if decision in {'EXPIRED', 'CLOSED'} else None, self.review_token)
        self.root.quit()

    def check_session(self):
        if self.result is not None:
            return
        try:
            self.verify()
        except Exception:
            self.result = _DevFileReviewOutcome('UNAVAILABLE', 'SESSION_GUARD')
            self.root.quit()
            return
        self.root.after(100, self.check_session)

    def finish(self):
        if self.result is None:
            return _DevFileReviewOutcome('UNAVAILABLE', 'NO_DECISION')
        if self.result.decision == 'APPROVED':
            try:
                if self.verify is not None:
                    self.verify()
            except Exception:
                return _DevFileReviewOutcome('UNAVAILABLE', 'SESSION_GUARD')
            if monotonic() >= self.deadline:
                return _DevFileReviewOutcome('UNAVAILABLE', 'LATE_RETURN')
        return self.result


def _dev_file_review_stop(observed, guard, deadline, token, path, old, new, op):
    # UI return and final Owner verification precede *all* write admission.
    error = {'status': 'ERROR', 'error': 'ReviewUnavailable',
             'write_outcome': 'NOT_STARTED', 'auto_retry': False}
    try:
        guard.verify()
    except Exception:
        return error
    if type(observed) is not _DevFileReviewOutcome:
        return error
    if observed.decision in {'EXPIRED', 'CLOSED'}:
        at = observed.timer_at
        if (observed.reason != 'LOCAL_EVENT' or observed.review_token is not token
                or observed.deadline != deadline or type(at) not in (int, float)
                or type(deadline) not in (int, float) or not isfinite(deadline)
                or not isfinite(at) or at > monotonic()
                or (observed.decision == 'EXPIRED' and at < deadline)):
            return error
        return _VerifiedFileReviewExpiry(token, path, old, new, op,
            reason=observed.decision, deadline=deadline, timer_at=at)
    if observed.decision == 'REJECT':
        return {'status': 'DENIED'}
    if (observed.decision != 'APPROVED' or monotonic() >= deadline
            or observed.review_token is not token or observed.deadline != deadline):
        return error
    return None


def review_and_create_timeout_dev(path, roots, content, expected_sha256, *, review_token,
                                  verify_binding=None, locale="en"):
    """Local DEV pilot only. Same single review; no reusable grant."""
    if type(review_token) is not object:
        raise ValueError('local invocation token required')
    return review_and_create_local(path, roots, content, expected_sha256,
                                   _dev_timeout_token=review_token,
                                   _dev_verify_binding=verify_binding, locale=locale)


def review_and_replace_timeout_dev(path, roots, content, expected_sha256, *, review_token,
                                   verify_binding=None, locale="en"):
    """Local DEV pilot only. Legacy callers retain their exact result shapes."""
    if type(review_token) is not object:
        raise ValueError('local invocation token required')
    return review_and_replace_local(path, roots, content, expected_sha256,
                                    _dev_timeout_token=review_token,
                                    _dev_verify_binding=verify_binding, locale=locale)


def _render_create_review(preview, expected_sha256, *, deadline, verify=None,
                          _review_token=None, locale="en"):
    """One existing CREATE window; caller owns validation and write admission."""
    import tkinter as tk
    from . import approval_visual_theme as theme
    root = tk.Tk()
    selected = _FileReviewDecision(root, deadline, verify, _review_token)
    try:
        root.title('Sora Commander Next - LOCAL file creation review')
        theme.window(root)
        card = theme.header(tk, root, 'LOCAL file create — Review complete JSON-escaped UTF-8 text.')
        from .owner_review_i18n import load_catalog
        words = load_catalog(locale)
        theme.notice(tk, card, words['pilot_file_caution'], caution=True)
        theme.notice(tk, card, words['pilot_file_information'])
        pane = theme.viewer(tk, card)
        pane.insert('1.0', f"{escape_review_display(preview['path'])}\n{preview['bytes']} bytes\nSHA-256: {expected_sha256}\n\n" + preview['review'])
        pane.config(state='disabled')
        theme.review_actions(tk, card, lambda: selected.decide('REJECT'),
                             lambda: selected.decide('APPROVED'))
        root.protocol('WM_DELETE_WINDOW', lambda: selected.decide('CLOSED'))
        root.after(max(0, ceil((deadline - monotonic()) * 1000) + 25),
                   lambda: selected.decide('EXPIRED'))
        if verify is not None:
            root.after(0, selected.check_session)
            # Non-blocking Windows system alert when the Owner review appears.
        # Audio failure must never approve, deny, or change the expiry clock.
        def alert_review():
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                try:
                    root.bell()
                except Exception:
                    pass
        root.after(100, alert_review)
        root.mainloop()
    finally:
        root.destroy()
    return selected.finish()


def _show_create_review_dev(preview, expected_sha256, *, deadline=None, verify=None,
                            _review_token=None, locale="en"):
    """Opt-in DEV observation of one CREATE UI, no path admission or writes.

    Trusted local callers only, never registered as an MCP/Host capability.
    Exceptions remain exceptions on legacy paths; this seam records UI ERROR.
    """
    if deadline is None:
        deadline = monotonic() + 45
    try:
        return _render_create_review(preview, expected_sha256,
                                     deadline=deadline, verify=verify,
                                     _review_token=_review_token, locale=locale)
    except Exception:
        return _DevFileReviewOutcome('ERROR', 'UI_ERROR')


def review_and_create_local(path, roots, content, expected_sha256, *,
                            _dev_timeout_token=None, _dev_verify_binding=None, locale="en"):
    """One bounded Owner-session approval for one absent disposable file."""
    from .windows_owner import OwnerSessionGuard
    guard = OwnerSessionGuard()
    guard.verify()
    preview = preview_text_create(path, roots, content)
    if preview['review_truncated'] or preview['new_sha256'] != expected_sha256:
        raise ReadBoundaryError('incomplete or changed create proposal')
    deadline = monotonic() + 45
    if _dev_timeout_token is not None:
        observed = _show_create_review_dev(
            preview, expected_sha256, deadline=deadline, verify=guard.verify,
            _review_token=_dev_timeout_token, locale=locale)
        stopped = _dev_file_review_stop(observed, guard, deadline,
            _dev_timeout_token, path, None, expected_sha256, 'create')
        if stopped is not None:
            return stopped
    else:
        observed = _render_create_review(preview, expected_sha256, deadline=deadline)
    if observed.decision != 'APPROVED' or monotonic() >= deadline:
        if _dev_timeout_token is not None:
            return {'status': 'ERROR', 'error': 'LateApproval', 'auto_retry': False}
        return {'status': 'DENIED'}
    guard.verify()
    if monotonic() >= deadline:
        if _dev_timeout_token is not None:
            return {'status': 'ERROR', 'error': 'LateApproval', 'auto_retry': False}
        return {'status': 'DENIED'}

    def before_create(_path):
        try:
            guard.verify()
        except PermissionError as exc:
            raise WriteNotStartedError('Owner identity changed before create') from exc
        if monotonic() >= deadline:
            raise WriteNotStartedError('Owner review expired before create')
        if _dev_timeout_token is not None and _dev_verify_binding is not None:
            _dev_verify_binding()

    # Only explicitly typed pre-create rejections may report NOT_STARTED.
    if _dev_timeout_token is not None and _dev_verify_binding is not None:
        _dev_verify_binding()
    return create_text_exclusive(path, roots, content, expected_sha256,
                                 _before_create=before_create)


def _require_inherited_windows_acl(path):
    """Fail closed on explicit file ACL entries; this is not a security sandbox."""
    if os.name != 'nt':
        return
    script = ('$ErrorActionPreference="Stop"; '
              '$acl=Get-Acl -LiteralPath $env:SORA_NEXT_ACL_TARGET; '
              'if (@($acl.Access | Where-Object { -not $_.IsInherited }).Count -gt 0) '
              '{ exit 3 }')
    try:
        env = {**os.environ, 'SORA_NEXT_ACL_TARGET': str(path)}
        # PowerShell 7 adds incompatible module paths to inherited PSModulePath.
        # Resolve Windows PowerShell 5.1's machine-level path instead of
        # disabling ACL validation or altering the calling process environment.
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment') as key:
            machine_modules, _ = winreg.QueryValueEx(key, 'PSModulePath')
        env['PSModulePath'] = os.path.expandvars(machine_modules)
        # Resolve Windows PowerShell 5.1 from the installed Windows volume,
        # not the process PATH (which may prioritize a Store/MSIX alias).
        system_root = os.environ.get('SystemRoot')
        if not system_root or not Path(system_root).is_absolute():
            raise ReadBoundaryError('Windows SystemRoot unavailable')
        ps51 = Path(system_root) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
        result = subprocess.run([str(ps51), '-NoProfile', '-NonInteractive',
                                 '-Command', script], capture_output=True, timeout=15,
                                env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadBoundaryError('cannot verify Windows ACL') from exc
    if result.returncode != 0:
        raise ReadBoundaryError('unsupported or unverifiable Windows ACL')


def replace_text(path, roots, content, expected_sha256, *, _before_commit=None):
    """Stage then replace only if exact bytes still match (cooperative CAS)."""
    if type(content) is not str:
        raise ReadBoundaryError("UTF-8 text required")
    data = content.encode("utf-8")
    if len(data) > 65536:
        raise ReadBoundaryError("invalid write size")
    if (type(expected_sha256) is not str or len(expected_sha256) != 64
            or any(c not in "0123456789abcdef" for c in expected_sha256)):
        raise ReadBoundaryError("expected SHA256 required")
    p = _check(path, roots)
    info = p.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536 or info.st_nlink != 1:
        raise ReadBoundaryError("ordinary small single-link file required")
    before = p.read_bytes()
    if sha256(before).hexdigest() != expected_sha256:
        raise WriteNotStartedError("file changed since read")
    # Avoid touching timestamps, ACLs or watcher state for identical content.
    if data == before:
        return {"status": "UNCHANGED_LOCAL_DEV", "bytes": len(data),
                "sha256": expected_sha256}
    _require_inherited_windows_acl(p)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=p.parent, prefix=".sora-next-",
                                         suffix=".tmp", delete=False) as out:
            staged = Path(out.name)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        # Re-check path, identity and content immediately before replacement.
        verified = _check(path, roots)
        again = verified.stat()
        if (verified != p or (info.st_dev, info.st_ino) != (again.st_dev, again.st_ino)
                or again.st_nlink != 1 or sha256(verified.read_bytes()).hexdigest() != expected_sha256):
            raise WriteNotStartedError("file changed before replacement")
        # Optional stricter local adapter revalidation; never supplied over MCP.
        if _before_commit is not None:
            _before_commit(p, staged)
        os.replace(staged, p)
        staged = None
        return {"status": "REPLACED_LOCAL_DEV", "bytes": len(data),
                "sha256": sha256(data).hexdigest()}
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def preview_text_edit(path, roots, content, expected_sha256):
    """Local preview only; no modification and no approval authority."""
    if type(content) is not str or len(content.encode('utf-8')) > 65536:
        raise ReadBoundaryError('invalid proposed content')
    p = _check(path, roots)
    before = _read_inspection_text(path, roots, 65536)
    if before['sha256'] != expected_sha256:
        raise StalePreviewError('file changed since read')
    old = before['content']
    diff = ''.join(difflib.unified_diff(old.splitlines(keepends=True),
        content.splitlines(keepends=True), fromfile='current', tofile='proposed'))
    return {'path': str(p), 'old_sha256': expected_sha256,
            'new_sha256': sha256(content.encode('utf-8')).hexdigest(),
            'changed': old != content, 'diff': diff[:32768],
            'diff_truncated': len(diff) > 32768}


def review_and_replace_local(path, roots, content, expected_sha256, *,
                             _dev_timeout_token=None, _dev_verify_binding=None, locale="en"):
    """Interactive Windows OWNER-SESSION dev pilot only; never an MCP tool.

    In-process UI is NOT a protected security boundary against same-user code.
    Deny on invalid identity, incomplete preview, close or timeout.
    """
    from .windows_owner import OwnerSessionGuard
    try:
        guard = OwnerSessionGuard()
        guard.verify()
        preview = preview_text_edit(path, roots, content, expected_sha256)
    except (PermissionError, ReadBoundaryError) as exc:
        # All checks above are read-only; no write boundary was entered.
        raise WriteNotStartedError('Owner or preview rejected before write') from exc
    if not preview['changed']:
        return {'status': 'UNCHANGED_LOCAL_DEV'}
    if preview['diff_truncated']:
        raise WriteNotStartedError('diff too large to review safely')
    deadline = monotonic() + 45
    if _dev_timeout_token is not None:
        observed = _show_edit_review_dev(preview, deadline=deadline,
            verify=guard.verify, _review_token=_dev_timeout_token, locale=locale)
        stopped = _dev_file_review_stop(observed, guard, deadline,
            _dev_timeout_token, path, expected_sha256, preview['new_sha256'], 'edit')
        if stopped is not None:
            return stopped
    elif not _show_edit_review(preview, deadline=deadline):
        return {'status': 'DENIED'}
    try:
        guard.verify()
    except PermissionError as exc:
        raise WriteNotStartedError('Owner identity changed before write') from exc
    if monotonic() >= deadline:
        if _dev_timeout_token is not None:
            return {'status': 'ERROR', 'error': 'LateApproval', 'auto_retry': False}
        return {'status': 'DENIED'}

    def before_commit(_path, _staged):
        try:
            guard.verify()
        except PermissionError as exc:
            raise WriteNotStartedError('Owner identity changed before replacement') from exc
        if monotonic() >= deadline:
            raise WriteNotStartedError('Owner review expired before replacement')
        if _dev_timeout_token is not None and _dev_verify_binding is not None:
            _dev_verify_binding()

    # Only explicitly typed pre-replace rejections may report NOT_STARTED.
    # Any other exception (notably a failed os.replace) stays UNKNOWN.
    if _dev_timeout_token is not None and _dev_verify_binding is not None:
        _dev_verify_binding()
    return replace_text(path, roots, content, expected_sha256,
                        _before_commit=before_commit)


def _show_edit_review(preview, *, complete_review=None, deadline=None,
                      title='LOCAL file edit review',
                      wording='One-time local file edit. Inspect the complete diff.',
                      verify=None):
    """Legacy exact bool API; C4 interactive consumers use the typed sibling."""
    observed = _render_edit_review(
        preview, complete_review=complete_review, deadline=deadline,
        title=title, wording=wording, verify=verify)
    return observed.decision == 'APPROVED'


def _show_edit_review_dev(preview, **options):
    """Opt-in DEV UI observation only; same window, no write authority.

    Options are the existing trusted local renderer arguments, never wire input.
    This does not publish a new file-edit/create Host or MCP result contract.
    """
    try:
        return _render_edit_review(preview, **options)
    except Exception:
        return _DevFileReviewOutcome('ERROR', 'UI_ERROR')


def _render_edit_review(preview, *, complete_review=None, deadline=None,
                        title='LOCAL file edit review',
                        wording='One-time local file edit. Inspect the complete diff.',
                        verify=None, _review_token=None, locale="en",
                        _notice_keys=("pilot_file_caution", "pilot_file_information")):
    """Shared local GUI only. No write or reusable approval is returned remotely."""
    # Tk timers can run late: bind the default budget before GUI setup and
    # check the monotonic deadline again after the event loop returns.
    if deadline is None:
        deadline = monotonic() + 45
    import tkinter as tk
    from . import approval_visual_theme as theme
    root = tk.Tk()
    try:
        root.title('Sora Commander Next - ' + title)
        theme.window(root)
        card = theme.header(tk, root, title + ' — ' + wording)
        from .owner_review_i18n import load_catalog
        words = load_catalog(locale)
        theme.notice(tk, card, words[_notice_keys[0]], caution=True)
        theme.notice(tk, card, words[_notice_keys[1]])
        pane = theme.viewer(tk, card)
        pane.insert('1.0', escape_review_display(preview['path']) + '\n\n')
        pane.insert('end', escape_review_display(
            preview['diff'] if complete_review is None else complete_review))
        pane.config(state='disabled')
        selected = _FileReviewDecision(root, deadline, verify, _review_token)
        theme.review_actions(tk, card, lambda: selected.decide('REJECT'),
                             lambda: selected.decide('APPROVED'))
        root.protocol('WM_DELETE_WINDOW', lambda: selected.decide('CLOSED'))
        remaining = max(0, ceil((deadline - monotonic()) * 1000) + 25)
        root.after(remaining, lambda: selected.decide('EXPIRED'))
        if verify is not None:
            root.after(0, selected.check_session)
            # Non-blocking Windows system alert when the Owner review appears.
        # Audio failure must never approve, deny, or change the expiry clock.
        def alert_review():
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                try:
                    root.bell()
                except Exception:
                    pass
        root.after(100, alert_review)
        root.mainloop()
    finally:
        root.destroy()
    return selected.finish()


# D06: General repository source CREATE PREVIEW ONLY.
# This helper does not create files, grant an Owner approval, cache a consent
# decision, register a writer, or turn a preview into a write capability.
_SOURCE_EXTENSIONS = frozenset({
    ".py", ".pyi", ".md", ".txt", ".json", ".toml", ".yaml", ".yml",
    ".js", ".jsx", ".ts", ".tsx", ".rs", ".go", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".sql", ".css", ".html", ".xml",
    ".ps1", ".sh", ".ini", ".cfg",
})


def preview_source_create(path, roots, content):
    """Review exact bytes of ONE proposed absent source file; NEVER write.

    Owner-reviewed execution must later revalidate absence, identity, scope,
    proposal digest and protected Broker authority. This preview is not a
    reservation or a TOCTOU-safe authorization.
    """
    if (type(path) is not str or not path or "\x00" in path
            or type(content) is not str):
        raise ReadBoundaryError("invalid source create preview")
    p = Path(path)
    name = p.name
    # Never preview creating files in repository metadata or build trees.
    private_parts = {".git", ".venv", "node_modules", "dist", "build",
                     "__pycache__", ".next", "target"}
    if any(part.startswith(".") or part.casefold() in private_parts
           for part in p.parts[1:-1]):
        raise ReadBoundaryError("managed or hidden source directory")
    if (not p.is_absolute() or not name or name.startswith(".")
            or name.endswith((" ", "."))
            or any(c in name for c in '<>:"/\\|?*')
            or p.suffix.lower() not in _SOURCE_EXTENSIONS
            or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?",
                            name) is not None):
        raise ReadBoundaryError("unsupported source filename")
    # Parents must ALREADY exist under Owner's configured read scope.
    # This rejects parent symlinks/junctions through the existing _check.
    parent = _check(str(p.parent), roots)
    if parent != p.parent or not parent.is_dir():
        raise ReadBoundaryError("source parent not in an approved root")
    try:
        p.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ReadBoundaryError("source target already exists")
    try:
        data = content.encode("utf-8")
    except UnicodeError as exc:
        raise ReadBoundaryError("invalid UTF-8 proposal") from exc
    if len(data) > 32768:
        raise ReadBoundaryError("source create preview too large")
    review = json.dumps({
        "operation": "propose creating exactly ONE absent source file",
        "path": str(p), "new_sha256": sha256(data).hexdigest(),
        "content": content,
    }, ensure_ascii=True, indent=2)
    diff = "".join(difflib.unified_diff(
        [], content.splitlines(keepends=True),
        fromfile="absent", tofile="proposed",
    ))
    if len(review) > 32768 or len(diff) > 32768:
        raise ReadBoundaryError("incomplete creation preview")
    # A second read-only check narrows but cannot close the race to any
    # FUTURE protected/authorized creation operation.
    try:
        p.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ReadBoundaryError("source target appeared during preview")
    if _check(str(p.parent), roots) != parent:
        raise ReadBoundaryError("source parent changed during preview")
    return {
        "status": "PREVIEW_ONLY", "operation": "create",
        "path": str(p), "bytes": len(data),
        "new_sha256": sha256(data).hexdigest(),
        "diff": diff, "full_escaped_review": review,
        "creation_authorized": False,
    }

# D06: create a complete *read-only* source change proposal from one literal
# anchor. Never send this result to an unprotected writer as permission to edit.
# The source cap equals the existing replace_text/preview_text_edit writer cap,
# so it grants no new write size. What the Owner must review stays bounded by
# the separate diff and escaped-review caps, not by the source size.
SOURCE_PATCH_MAX_SOURCE = 65536
SOURCE_PATCH_MAX_DIFF = 16384


def literal_occurs_once(text, anchor):
    """True only for exactly one match, counting overlapping matches.

    str.count() skips overlaps: "aa" occurs twice in "aaa" but counts once.
    """
    first = text.find(anchor)
    return first != -1 and text.find(anchor, first + 1) == -1


def preview_source_patch(path, roots, old_text, new_text, expected_sha256):
    """Preview one unambiguous literal replacement against a known source SHA.

    A successful preview is NOT Owner authorization or a write reservation.
    A future protected Broker must independently verify the precise target,
    reviewed full content and current SHA immediately at its commit boundary.
    """
    from .file_reads import _read_inspection_text
    if (type(path) is not str or not path or "\x00" in path
            or type(old_text) is not str or not old_text
            or type(new_text) is not str
            or type(expected_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None):
        raise ReadBoundaryError("invalid source patch request")
    try:
        anchor_bytes = old_text.encode("utf-8")
        replacement_bytes = new_text.encode("utf-8")
    except UnicodeError as exc:
        raise ReadBoundaryError("invalid patch Unicode") from exc
    if len(anchor_bytes) > 4096 or len(replacement_bytes) > 4096:
        raise ReadBoundaryError("patch anchor/replacement limit exceeded")
    p = Path(path)
    if (not p.is_absolute() or p.suffix.casefold() not in _SOURCE_EXTENSIONS
            or any(part.startswith(".") or part.casefold() in {
                ".git", ".venv", "node_modules", "build", "dist", "__pycache__",
                ".next", "target"} for part in p.parts[1:-1])):
        raise ReadBoundaryError("unsupported source patch target")
    snapshot = _read_inspection_text(path, roots, SOURCE_PATCH_MAX_SOURCE)
    if snapshot["sha256"] != expected_sha256:
        raise StalePreviewError("source changed since inspection")
    before = snapshot["content"]
    if not literal_occurs_once(before, old_text):
        raise ReadBoundaryError("patch anchor must occur exactly once")
    content = before.replace(old_text, new_text, 1)
    encoded = content.encode("utf-8")
    if len(encoded) > SOURCE_PATCH_MAX_SOURCE:
        raise ReadBoundaryError("resulting source exceeds patch limit")
    preview = preview_text_edit(path, roots, content, expected_sha256)
    if (preview["diff_truncated"] or preview["new_sha256"] != sha256(encoded).hexdigest()
            or preview["changed"] != (before != content)
            or len(preview["diff"].encode("utf-8")) > SOURCE_PATCH_MAX_DIFF):
        raise ReadBoundaryError("incomplete or inconsistent patch preview")
    # The complete changed sections are displayed as escaped JSON so controls,
    # CRLF and final newlines are never invisible in Owner review.
    review = json.dumps({
        "operation": "preview one existing source change; NOT authorized",
        "path": str(p), "old_sha256": expected_sha256,
        "new_sha256": preview["new_sha256"],
        "anchor_before": old_text, "anchor_after": new_text,
        "diff": preview["diff"],
    }, ensure_ascii=True, indent=2)
    if len(review) > 32768:
        raise ReadBoundaryError("complete source patch review too large")
    # Old digest must still be true at the end of the preview. This narrows
    # races but cannot authenticate a writer or guarantee future file identity.
    verify = _read_inspection_text(path, roots, SOURCE_PATCH_MAX_SOURCE)
    if verify["sha256"] != expected_sha256 or verify["content"] != before:
        raise StalePreviewError("source changed during preview")
    return {
        "status": "PREVIEW_ONLY",
        "operation": "replace_literal_once",
        "path": str(p),
        "old_sha256": expected_sha256,
        "new_sha256": preview["new_sha256"],
        "changed": preview["changed"],
        "content": content,
        "diff": preview["diff"],
        "complete_escaped_review": review,
        "creation_authorized": False,
        "write_authorized": False,
    }
