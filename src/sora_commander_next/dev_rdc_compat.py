"""Personal DEV_ONLY cooperative filesystem operations, never Broker authority.

Local Owner GUI is required on every mutation. These checks narrow races but
do not provide hostile-writer CAS, durable outcomes, retries or rollback.
"""
import json
import os
from pathlib import Path
import stat
from dataclasses import dataclass
from time import monotonic
from hashlib import sha256

from . import file_edits
from .file_reads import _check, _read_name_denied, ReadBoundaryError


def _path(path):
    if (type(path) is not str or not 1 <= len(path) <= 1024
            or any(ord(c) < 32 for c in path)):
        raise ReadBoundaryError("invalid path")
    p = Path(path)
    if (not p.is_absolute() or path.startswith(("\\\\", "//"))
            or any(part in (".", "..") for part in path.replace("\\", "/").split("/"))
            or any(_read_name_denied(part) or any(c in part for c in '<>:"|?*')
                   or part.casefold() == "start-sora-commander.cmd"
                   for part in p.parts[1:])):
        raise ReadBoundaryError("ambiguous or protected path")
    return p


def _existing(path, roots, *, directory=False):
    p = _path(path)
    # lstat all ancestors, including the configured root: Windows reparse
    # attributes also cover junctions when Path.is_junction is unavailable.
    for entry in (p, *p.parents):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ReadBoundaryError("linked path")
    checked = _check(path, roots)
    info = checked.stat()
    if checked != p or (not stat.S_ISDIR(info.st_mode) if directory else
                        not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise ReadBoundaryError("unsupported target")
    return checked, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _absent(path, roots):
    p = _path(path)
    parent, identity = _existing(str(p.parent), roots, directory=True)
    try:
        p.lstat()
    except FileNotFoundError:
        return p, (parent, identity)
    raise ReadBoundaryError("destination exists")


def _new_text_bytes(content):
    if type(content) is not str:
        raise ReadBoundaryError("UTF-8 text required")
    data = content.encode("utf-8", errors="strict")
    if len(data) > 65536:
        raise ReadBoundaryError("invalid create size")
    return data


def _write_new_text_file(target, data):
    """Exclusive one-shot create; never overwrites or claims a rollback."""
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_BINARY", 0), 0o666)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short create write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _owner():
    from .windows_owner import OwnerSessionGuard
    guard = OwnerSessionGuard()
    guard.verify()
    return guard


def _review(guard, proposal, *, deadline=None, verify=None):
    deadline = min(monotonic() + 45, deadline) if deadline is not None else monotonic() + 45
    if monotonic() >= deadline:
        return None
    options = {}
    if verify is not None:
        verify()
        options = {'title': 'LOCAL interactive Python launch review',
                   'wording': 'Second approval: launch interactive Python with stdin PIPE. Inspect the exact command.',
                   'verify': verify}
    approved = file_edits._show_edit_review(
        {"path": "DEV_ONLY interactive Python launch" if verify is not None
                 else "DEV_ONLY local filesystem review", "diff": ""},
        complete_review=json.dumps(proposal, ensure_ascii=True, indent=2),
        deadline=deadline, **options)
    if approved is not True or monotonic() >= deadline:
        return None
    guard.verify()
    return deadline if monotonic() < deadline else None


def _review_typed(guard, proposal, *, deadline=None, verify=None):
    """Reuse the file window's invocation-bound proof before any effect."""
    deadline = min(monotonic() + 45, deadline) if deadline is not None else monotonic() + 45
    token = object()
    path = 'DEV_ONLY interactive Python launch' if verify is not None else 'DEV_ONLY stdin input'
    serialized = json.dumps(proposal, ensure_ascii=True, indent=2)
    digest = sha256(serialized.encode('utf-8')).hexdigest()
    options = {}
    if verify is not None:
        verify()
        options = {'title': 'LOCAL interactive Python launch review',
                   'wording': 'Second approval: launch interactive Python with stdin PIPE. Inspect the exact command.',
                   'verify': verify}
    if monotonic() >= deadline:
        raise ReadBoundaryError('review unavailable')
    observed = file_edits._show_edit_review_dev(
        {'path': path, 'diff': ''}, complete_review=serialized,
        deadline=deadline, _review_token=token,
        _notice_keys=('interactive_caution', 'interactive_information'), **options)
    stopped = file_edits._dev_file_review_stop(
        observed, guard, deadline, token, path, None, digest, 'interactive_review')
    if type(stopped) is file_edits._VerifiedFileReviewExpiry:
        return stopped
    if stopped is None:
        return deadline
    if stopped == {'status': 'DENIED'}:
        return None
    raise ReadBoundaryError('review unavailable')


def _review_file(guard, proposal):
    """The Japanese window, for the four file operations in mutate only.

    C4 interactive Python and stdin use _review_typed; legacy _review is unchanged.
    A failed file window refuses, with no fallback to the old window. APPROVE is a local answer only:
    mutate keeps every check it has before writing.
    """
    from .owner_file_review_gate_dev_20261105 import review_once
    deadline = monotonic() + 45
    token = object()
    decision = review_once(guard, proposal, seconds=45.0, review_token=token)
    if type(decision) is file_edits._VerifiedFileReviewExpiry:
        guard.verify()
        digest = sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()
        if (decision.review_token is not token or decision.reason not in {"EXPIRED", "CLOSED"}
                or decision.write_outcome != "NOT_STARTED"
                or decision.operation != proposal["operation"]
                or decision.path != proposal.get("path", proposal.get("source"))
                or decision.old_sha256 != proposal.get("old_sha256")
                or decision.new_sha256 != digest):
            raise ReadBoundaryError("file review unavailable")
        return decision
    if decision == "DENY":
        return None
    if decision != "APPROVE" or monotonic() >= deadline:
        raise ReadBoundaryError("file review unavailable")
    guard.verify()
    if monotonic() >= deadline:
        raise ReadBoundaryError("file review unavailable")
    return deadline


def mutate(operation, roots, **arguments):
    """Safe response even when a filesystem call's outcome is uncertain."""
    started = False
    try:
        guard = _owner()
        if operation == "create_directory":
            target, parent = _absent(arguments["path"], roots)
            proposal = {"operation": operation, "path": str(target)}
        elif operation == "create_file":
            target, parent = _absent(arguments["path"], roots)
            data = _new_text_bytes(arguments["content"])
            proposal = {"operation": operation, "path": str(target),
                        "bytes": len(data), "sha256": sha256(data).hexdigest(),
                        "full_proposed_content": arguments["content"]}
        elif operation == "move_file":
            if os.name != "nt":
                raise ReadBoundaryError("non-overwriting Windows rename required")
            source, identity = _existing(arguments["source"], roots)
            target, parent = _absent(arguments["destination"], roots)
            if source == target or source in target.parents:
                raise ReadBoundaryError("self move")
            proposal = {"operation": operation, "source": str(source), "destination": str(target)}
        elif operation == "request_source_patch_dev":
            _existing(arguments["path"], roots)
            preview = file_edits.preview_source_patch(roots=roots, **arguments)
            proposal = {"operation": operation, "path": preview["path"],
                        "old_sha256": preview["old_sha256"],
                        "new_sha256": preview["new_sha256"],
                        "diff": preview["diff"], "full_proposed_content": preview["content"]}
        else:
            raise ReadBoundaryError("unsupported operation")
        try:
            deadline = _review_file(guard, proposal)
        except Exception:
            return {"status": "ERROR", "error": "ReviewUnavailable",
                    "write_outcome": "NOT_STARTED", "auto_retry": False,
                    "required_action": "REPORT_AND_WAIT"}
        if type(deadline) is file_edits._VerifiedFileReviewExpiry:
            return {"status": "TIMED_OUT", "stage": "FILE_GUI", "reason": deadline.reason,
                    "approval_state": "EXPIRED", "write_outcome": "NOT_STARTED",
                    "auto_retry": False, "required_action": "REPORT_AND_WAIT"}
        if deadline is None:
            return {"status": "DENIED", "outcome": "NOT_STARTED"}
        def verify_owner():
            guard.verify()
            if monotonic() >= deadline:
                raise ReadBoundaryError("review expired")
        if operation == "request_source_patch_dev":
            def recheck(_target=None, _staged=None):
                # This gate runs only before any write (standalone, and as
                # replace_text's pre-commit hook), so a boundary failure here
                # — e.g. StalePreviewError when the source changed since the
                # reviewed preview — proves the target is still unwritten
                # and is reported as NOT_STARTED, never UNKNOWN (P3-4).
                try:
                    verify_owner()
                    _existing(arguments["path"], roots)
                    fresh = file_edits.preview_source_patch(roots=roots, **arguments)
                except ReadBoundaryError as exc:
                    raise file_edits.WriteNotStartedError("proposal changed") from exc
                if fresh != preview:
                    raise file_edits.WriteNotStartedError("proposal changed")
            recheck()
            # replace_text performs an additional SHA/identity check and calls
            # recheck immediately before os.replace. Unknown exceptions must
            # never imply a verified rollback or authorize a second attempt.
            started = True
            result = file_edits.replace_text(arguments["path"], roots,
                preview["content"], arguments["expected_sha256"], _before_commit=recheck)
            verified = file_edits._read_inspection_text(
                arguments["path"], roots, file_edits.SOURCE_PATCH_MAX_SOURCE)
            if verified["sha256"] != preview["new_sha256"]:
                raise RuntimeError("post-write mismatch")
            return result
        if _absent(str(target), roots) != (target, parent):
            raise ReadBoundaryError("parent changed")
        verify_owner()
        if operation == "move_file" and _existing(str(source), roots) != (source, identity):
            raise ReadBoundaryError("source changed")
        started = True
        if operation == "create_directory":
            target.mkdir()  # no parents, no exist_ok, no automatic rollback
            _existing(str(target), roots, directory=True)
            return {"status": "CREATED_LOCAL_DEV", "outcome": "VERIFIED_LOCAL_DEV"}
        if operation == "create_file":
            _write_new_text_file(target, data)
            _existing(str(target), roots)
            verified = file_edits._read_inspection_text(str(target), roots, 65536)
            if verified["sha256"] != sha256(data).hexdigest():
                raise RuntimeError("create verification failed")
            return {"status": "CREATED_LOCAL_DEV", "outcome": "VERIFIED_LOCAL_DEV"}
        # Windows rename fails if the destination exists; never os.replace.
        os.rename(source, target)
        moved, after = _existing(str(target), roots)
        if after != identity or source.exists():
            raise RuntimeError("move verification failed")
        return {"status": "MOVED_LOCAL_DEV", "outcome": "VERIFIED_LOCAL_DEV"}
    except file_edits.WriteNotStartedError:
        # The cooperative writer raises this only before os.replace, so the
        # target is provably unwritten even when this attempt had started.
        return {"status": "REJECTED", "outcome": "NOT_STARTED",
                "error": "DevOperationFailed", "retry_allowed": False}
    except Exception:
        return {"status": "OUTCOME_UNKNOWN" if started else "REJECTED",
                "outcome": "UNKNOWN" if started else "NOT_STARTED",
                "error": "DevOperationFailed", "retry_allowed": False}


def _windows_process_rows():
    """Toolhelp process snapshot only: no handles to individual processes.

    ABI: Microsoft PROCESSENTRY32W / Process32FirstW / Process32NextW.
    Only PID and executable basename leave this helper. No retries.
    """
    if os.name != "nt":
        raise OSError("Windows process snapshot required")
    import ctypes as ct
    from ctypes import wintypes as wt
    class Entry(ct.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ct.c_size_t),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG),
                    ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]
    api = ct.WinDLL("kernel32", use_last_error=True)
    snapshot = api.CreateToolhelp32Snapshot
    snapshot.argtypes, snapshot.restype = [wt.DWORD, wt.DWORD], wt.HANDLE
    first, next_entry, close = api.Process32FirstW, api.Process32NextW, api.CloseHandle
    for function in (first, next_entry):
        function.argtypes, function.restype = [wt.HANDLE, ct.POINTER(Entry)], wt.BOOL
    close.argtypes, close.restype = [wt.HANDLE], wt.BOOL
    handle = snapshot(0x2, 0)  # TH32CS_SNAPPROCESS, no heap/module/thread snapshot
    if not handle or handle == ct.c_void_p(-1).value:
        raise OSError("snapshot unavailable")
    rows = []
    try:
        entry = Entry()
        entry.dwSize = ct.sizeof(Entry)
        current = first(handle, ct.byref(entry))
        while current:
            if len(rows) >= 4096:
                raise OSError("snapshot scan budget")
            rows.append({"pid": int(entry.th32ProcessID), "name": entry.szExeFile})
            current = next_entry(handle, ct.byref(entry))
        if ct.get_last_error() != 18:  # ERROR_NO_MORE_FILES is the only clean end
            raise OSError("snapshot incomplete")
    finally:
        if not close(handle):
            raise OSError("snapshot close failed")
    return rows


def list_processes(limit=100):
    """PID and bounded executable basename only; no process launch fallback."""
    try:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid limit")
        try:
            import psutil
        except ImportError:
            records = _windows_process_rows()
        else:
            records = (process.info for process in psutil.process_iter(attrs=["pid", "name"]))
        rows = []
        for visited, info in enumerate(records, 1):
            if visited > 4096:
                raise ValueError("process scan budget")
            pid, name = info.get("pid"), info.get("name")
            if (type(pid) is not int or not 0 <= pid <= 0xFFFFFFFF
                    or type(name) is not str or not 1 <= len(name) <= 128
                    or any(ord(c) < 32 or c in "/\\:" for c in name)):
                continue
            rows.append({"pid": pid, "name": name})
        rows.sort(key=lambda row: (row["pid"], row["name"]))
        return {"processes": rows[:limit], "truncated": len(rows) > limit}
    except Exception:
        return {"status": "ERROR", "error": "ProcessListingUnavailable"}


# Step 2: personal local DEV only. No generic REPL, shell or PID attachment.
class InteractivePreconditionFailed(PermissionError):
    """A DEV interactive admission precondition failed before any review.

    Subclasses PermissionError so every existing fail-closed handler keeps
    working unchanged. Host results report only the exception type name
    (messages are never echoed), so this distinct name is the stable,
    diagnosable label separating precondition failures from policy DENY.
    """


def validate_input(session_id, input_text, input_id):
    """Strict text wire contract plus a deliberately small numeric REPL subset."""
    import ast
    import unicodedata
    for value, size in ((session_id, 48), (input_id, 32)):
        if (type(value) is not str or len(value) != size
                or any(c not in '0123456789abcdef' for c in value)):
            raise ValueError('invalid input identity')
    if (type(input_text) is not str or not input_text or len(input_text) > 1024
            or any(c != '\n' and unicodedata.category(c).startswith('C') for c in input_text)):
        raise ValueError('invalid text')
    data = input_text.encode('utf-8', errors='strict')
    if len(data) > 1024 or not input_text.endswith('\n') or '\n' in input_text[:-1]:
        raise ValueError('one complete bounded line required')
    tree = ast.parse(input_text, mode='exec')
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Expr):
        raise ValueError('numeric expression required')
    expression = tree.body[0].value
    if isinstance(expression, ast.Call):
        if (not isinstance(expression.func, ast.Name) or expression.func.id != 'print'
                or len(expression.args) != 1 or expression.keywords):
            raise ValueError('only print of one numeric expression permitted')
        expression = expression.args[0]
    nodes = list(ast.walk(expression))
    if len(nodes) > 32:
        raise ValueError('expression too large')
    for node in nodes:
        if type(node) not in (ast.Constant, ast.BinOp, ast.UnaryOp,
                             ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
                             ast.Mod, ast.UAdd, ast.USub):
            raise ValueError('unsupported expression')
        if isinstance(node, ast.Constant) and (type(node.value) not in (int, float)
                or not -1000000 <= node.value <= 1000000):
            raise ValueError('bounded numeric literals required')
    return data


def _interactive_launch_shape(request, roots):
    import sys
    # The executable comparison is already symmetric: Command.__post_init__
    # stores absolute(executable) == str(Path(executable).resolve()), and the
    # Host side below is resolved the same way. Both sides must stay resolved;
    # the allowlist itself is intentionally exact and is not loosened.
    if (os.name != 'nt' or not request.interactive
            or request.executable != str(Path(sys.executable).resolve())
            or request.argv != ('-I', '-S', '-q', '-i')
            or not 0.01 <= request.timeout <= 120
            or len(request.request_id) != 32
            or any(c not in '0123456789abcdef' for c in request.request_id)):
        raise InteractivePreconditionFailed('interactive launch denied')
    return _existing(request.cwd, roots, directory=True)


def prepare_interactive(host, request, background, decision):
    """Narrow an existing REVIEW; never add a policy grant or approve it."""
    from .host import Host
    from .jobs import JobManager
    from .policy import Decision
    from .worker import execute
    manager = host._manager_for_request(request)
    if (type(host) is not Host or type(manager) is not JobManager
            or manager.protected_stage or manager.journal_path is not None
            or manager.output_archive_path is not None or host.worker is not execute
            or background is not True or decision != Decision.REVIEW
            or host._owner_review_dispatcher is None):
        raise InteractivePreconditionFailed('local DEV review required')
    roots = tuple(host.policy.roots)
    identity = _interactive_launch_shape(request, roots)
    guard = _owner()
    if host.owner != guard.sid:
        raise InteractivePreconditionFailed('Owner mismatch')
    with manager._lock:
        records = manager._dev_interactive_requests
        if request.request_id not in records:
            if len(records) >= 128:
                raise InteractivePreconditionFailed('interactive review capacity')
            records[request.request_id] = {
                'request': request, 'roots': roots, 'identity': identity,
                'guard': guard, 'expires': monotonic() + 60, 'authorized': False}
        elif records[request.request_id]['request'] != request:
            raise InteractivePreconditionFailed('request conflict')


@dataclass(frozen=True)
class _LaunchConsent:
    deadline: float
    verify: object


def authorize_interactive_local(host, request, *, deadline=None, verify=None, before_launch=None):
    """Called only AFTER existing local Owner approval was consumed.

    An additional explicit DEV GUI describes PIPE mode and exact launch.
    It runs outside the manager lock so stop/close can proceed during review.
    Existing approval records and decisions are never modified here.
    """
    manager = host._manager_for_request(request)
    with manager._lock:
        record = manager._dev_interactive_requests.get(request.request_id)
        if (record is None or record['request'] != request or manager._closed
                or record.get('reviewing') or record['authorized']):
            raise PermissionError('missing interactive review')
        record['reviewing'] = True  # one GUI attempt, including denial/failure
        deadline = min(monotonic() + 45, record['expires'],
                       deadline if deadline is not None else record['expires'])
    guard = record['guard']
    guard.verify()
    proposal = {'operation': 'DEV_ONLY interactive Python numeric REPL launch',
                'request_id': request.request_id, 'interactive': True,
                'executable': request.executable, 'argv': request.argv,
                'cwd': request.cwd, 'timeout': request.timeout,
                'limits': 'Numeric expressions only; each stdin line needs a new GUI decision.'}
    verify = verify if verify is not None else guard.verify
    reviewed = _review_typed(guard, proposal, deadline=deadline, verify=verify)
    if type(reviewed) is file_edits._VerifiedFileReviewExpiry:
        from .interactive_hooks import InteractiveReviewNoAnswer
        raise InteractiveReviewNoAnswer(reviewed.reason)
    if reviewed is None:
        raise PermissionError('interactive launch denied')
    with manager._lock:
        if (manager._closed or monotonic() >= deadline
                or manager._dev_interactive_requests.get(request.request_id) is not record
                or _interactive_launch_shape(request, record['roots']) != record['identity']):
            raise PermissionError('interactive review stale')
        guard.verify()
        verify()
        record['authorized'] = _LaunchConsent(deadline, before_launch or verify)


def approve_interactive_launch(manager, request):
    """Consume the one-shot local launch ticket; no GUI under manager lock."""
    record = manager._dev_interactive_requests.pop(request.request_id, None)
    if (record is None or record['request'] != request
            or type(record['authorized']) is not _LaunchConsent or manager._closed
            or manager.protected_stage or manager.journal_path is not None
            or manager.output_archive_path is not None or monotonic() >= record['expires']
            or _interactive_launch_shape(request, record['roots']) != record['identity']):
        raise PermissionError('interactive launch not approved')
    record['guard'].verify()
    consent = record['authorized']
    consent.verify()
    if monotonic() >= min(record['expires'], consent.deadline):
        raise PermissionError('interactive launch expired')
    return record['guard']


def _active_input_job(manager, session_id):
    job = manager._lookup(session_id)
    if (manager._closed or not job.request.interactive or job.stdin_owner is None or job.stdin_uncertain
            or job.child.stdin is None or job.done.is_set() or job.cancel.is_set()
            or job.child.poll() is not None or monotonic() >= job.deadline):
        raise PermissionError('active owned interactive process required')
    job.stdin_owner.verify()
    # Quiet isolated Python emits only prompts in stderr before numeric input.
    # Any unexpected stderr disables further input, without echoing its bytes.
    with job.lock:
        stderr = b''.join(data for _, stream, data in job.chunks if stream == 'stderr')
    if stderr.replace(b'>>> ', b'').replace(b'... ', b''):
        raise PermissionError('unexpected stderr')
    return job


def input_status(manager, pending_id, wait_seconds=0):
    """Data-only observation; no submission or approval side effect."""
    record = manager._stdin_pending[pending_id]
    record['done'].wait(min(wait_seconds, 45.0))
    with manager._lock:
        return dict(record['result'])


def _publish_input_activity(manager, pending_id, session_id, state):
    try:
        from .activity import publish
        publish(getattr(manager, '_activity_store', None), pending_id=pending_id,
                session_id=session_id, operation_type='send_input', state=state, source='input')
    except Exception:
        try:
            manager._activity_store.invalidate()
        except Exception:
            pass


def send_input(manager, session_id, input_text, input_id):
    """Reserve before review; write once, never replay or persist input text."""
    from threading import Event, Thread
    import secrets
    try:
        data = validate_input(session_id, input_text, input_id)
        with manager._lock:
            if input_id in manager._stdin_attempts:
                return {'status': 'DENIED', 'error': 'InputIdConsumed', 'retry_allowed': False}
            if len(manager._stdin_attempts) >= 1024:
                raise PermissionError('input replay guard full')
            job = _active_input_job(manager, session_id)
            if any(not r['done'].is_set() for r in manager._stdin_pending.values()):
                raise PermissionError('input review busy')
            pending_id = secrets.token_hex(16)
            record = {'done': Event(), 'result': {
                'status': 'PENDING', 'pending_id': pending_id, 'retry_allowed': False}}
            manager._stdin_attempts[input_id] = pending_id
            manager._stdin_pending[pending_id] = record
        _publish_input_activity(manager, pending_id, session_id, 'PENDING')
        def once():
            attempted = False
            result = {'status': 'DENIED', 'pending_id': pending_id, 'retry_allowed': False}
            try:
                reviewed = _review_typed(job.stdin_owner, {
                    'operation': 'DEV_ONLY send stdin ONCE',
                    'session_id': session_id, 'request_id': job.request.request_id,
                    'input_id': input_id, 'exact_input': input_text,
                    'bytes': len(data)})  # JSON ensure_ascii makes all controls visible.
                if type(reviewed) is file_edits._VerifiedFileReviewExpiry:
                    result.update(status='TIMED_OUT', stage='INPUT_GUI', reason=reviewed.reason,
                                  approval_state='EXPIRED', write_outcome='NOT_STARTED',
                                  auto_retry=False, required_action='REPORT_AND_WAIT')
                elif reviewed is not None:
                    deadline = reviewed
                    with manager._lock:
                        if _active_input_job(manager, session_id) is not job or monotonic() >= deadline:
                            raise PermissionError('review stale')
                        # Nonblocking anonymous pipe, <=1024 bytes, ONE os.write.
                        # A short/error write is UNKNOWN and permanently consumed.
                        attempted = True
                        count = os.write(job.child.stdin.fileno(), data)
                        result['status'] = 'SENT' if count == len(data) else 'UNKNOWN'
            except Exception:
                result['status'] = 'UNKNOWN' if attempted else 'ERROR'
                if not attempted:
                    result.update(write_outcome='NOT_STARTED', auto_retry=False)
            finally:
                with manager._lock:
                    if result['status'] == 'UNKNOWN':
                        job.stdin_uncertain = True
                        job.cancel.set()
                    record['result'] = result
                    _publish_input_activity(manager, pending_id, session_id, result['status'])
                    record['done'].set()
        try:
            Thread(target=once, daemon=True, name='sora-dev-stdin-review').start()
        except Exception:
            with manager._lock:
                job.stdin_uncertain = True
                job.cancel.set()
                record['result'] = {'status': 'UNKNOWN', 'pending_id': pending_id, 'retry_allowed': False}
                _publish_input_activity(manager, pending_id, session_id, 'UNKNOWN')
                record['done'].set()
        return {'status': 'PENDING', 'pending_id': pending_id, 'retry_allowed': False}
    except Exception:
        return {'status': 'DENIED', 'error': 'DevInputDenied', 'retry_allowed': False}


def install_interactive_hooks(manager):
    """Install this DEV module's interactive hooks on one JobManager.

    Called only from the MCP adapter's exact-Host DEV registration block
    (or a test fixture). Release-side code never imports this module; it
    dispatches through interactive_hooks and fails closed when no hooks
    are installed, so a release candidate denies interactive requests
    instead of importing DEV code.
    """
    from .interactive_hooks import InteractiveHooks, install_hooks
    install_hooks(manager, InteractiveHooks(
        prepare_interactive=prepare_interactive,
        approve_interactive_launch=approve_interactive_launch,
        authorize_interactive_local=authorize_interactive_local,
        input_status=input_status))


def install_host_interactive(host):
    """Exact DEV Host composition only; never replace its protected manager."""
    from .host import Host
    from .jobs import JobManager
    if type(host) is not Host:
        raise PermissionError('DEV Host required')
    if host._dev_interactive_jobs is None:
        if type(host.jobs) is JobManager and host.jobs._closed:
            raise PermissionError('Host closed')
        manager = JobManager()
        manager._dev_interactive_only = True
        install_interactive_hooks(manager)
        host._dev_interactive_jobs = manager
