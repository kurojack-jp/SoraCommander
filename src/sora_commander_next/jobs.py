"""Bounded command supervision; Windows descendants confined by Job Objects."""

from dataclasses import dataclass, field
import base64
import hashlib
import json
import math
import os
import secrets
import subprocess
from threading import Event, RLock, Thread
from time import monotonic

from .commands import Command
from .interactive_hooks import approve_interactive_launch

# A finished session can expire, but its request ID must never become fresh
# again in the same process. The protected Broker ledger owns cross-restart
# uniqueness; this is a bounded subordinate defense against reconnect replay.
_MAX_RETIRED_REQUESTS = 65536

# Approved child execution environment: no arbitrary inherited credentials.
# Keys are copied from the parent without changing OS/process settings.
_CHILD_ENV_KEYS = frozenset({
    "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROCESSOR_ARCHITECTURE",
    "LANG", "LC_ALL", "PYTHONUTF8", "PYTHONIOENCODING",
    "GIT_CONFIG_NOSYSTEM", "HOME", "XDG_CONFIG_HOME",
})


def _child_environment(source=None):
    """Construct a fresh allowlisted process environment; never log values."""
    current = os.environ if source is None else source
    if not hasattr(current, "items"):
        raise TypeError("invalid environment source")
    return {key: value for key, value in current.items()
            if type(key) is str and type(value) is str
            and key.upper() in _CHILD_ENV_KEYS}


def _unknown_command_fingerprint(request):
    """Private exact-command identity; never persist or expose this digest."""
    values = (os.path.normcase(os.path.normpath(request.executable)),
              request.argv,
              os.path.normcase(os.path.normpath(request.cwd)))
    return hashlib.sha256(json.dumps(
        values, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).digest()

if os.name == "nt":
    from .windows_job import WindowsJob


def _discard_spawn(child, tree):
    """Best-effort cleanup; return whether every observed step succeeded.

    A cleanup failure can never justify a NOT_STARTED retry. Do not leak an
    unrelated close error over the original launch error.
    """
    complete = True
    if tree is not None:
        try:
            tree.close()  # kill descendants even if a later step failed
        except OSError:
            complete = False
    if child is not None:
        try:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            complete = False
        for pipe in (getattr(child, "stdin", None), child.stdout, child.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    complete = False
    return complete


class UnknownSession(LookupError):
    pass


class LaunchOutcomeUnknown(RuntimeError):
    """A launch attempt may have executed its command; outcome unknown.

    Raised by JobManager.start() when a failure occurs at or after the
    point where the child may have started executing: spawn failure,
    resume failure, supervision-start failure, or acceptance loss after
    supervision started. It is deliberately NOT an Owner authorization
    error. The request id stays uncertain for this process, so a resend
    is refused and no automatic retry may occur.

    Attributes: request_id, phase (one of "pre_spawn", "spawning",
    "spawned_suspended", "spawned_running", "resuming", "resumed",
    "supervised"), and the original failure as __cause__.

    Subclassing RuntimeError is deliberate: existing handlers that treat
    a failed launch as a generic runtime failure keep working, while
    callers that need the distinction catch this type first.
    """

    def __init__(self, request_id, phase, message):
        super().__init__(message)
        self.request_id = request_id
        self.phase = phase


class LaunchNotStarted(RuntimeError):
    """A launch attempt provably never executed its command.

    Only when the failure happened before the child could run: before
    spawn, or on Windows while the child was still CREATE_SUSPENDED
    (before attach_and_resume returned). The uncertain marker is
    released, so a later JobManager.start with the same request id is a
    first execution, not a replay. This does NOT revive a consumed Owner
    approval record: approval remains single-use, so an end-to-end
    REVIEW retry still needs a fresh approval.
    """

    def __init__(self, request_id, phase, message):
        super().__init__(message)
        self.request_id = request_id
        self.phase = phase


def _read_available(pipe):
    """Never block on a pipe inherited by an unsupported descendant."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        peek = kernel.PeekNamedPipe
        peek.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                         wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
        peek.restype = wintypes.BOOL
        available = wintypes.DWORD()
        if not peek(msvcrt.get_osfhandle(pipe.fileno()), None, 0, None,
                    ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in (109, 232):
                return b""
            raise ctypes.WinError(error)
        return os.read(pipe.fileno(), min(4096, available.value)) if available.value else None
    try:
        return os.read(pipe.fileno(), 4096)
    except BlockingIOError:
        return None


@dataclass
class _Job:
    session_id: str
    request: Command
    child: subprocess.Popen
    deadline: float
    tree: object | None = None
    lock: RLock = field(default_factory=RLock)
    cancel: Event = field(default_factory=Event)
    done: Event = field(default_factory=Event)
    thread: Thread | None = None
    status: str = "RUNNING"
    chunks: list = field(default_factory=list)
    captured: int = 0
    overflow: bool = False
    output_incomplete: bool = False
    exit_code: int | None = None
    finished_at: float | None = None
    stdin_owner: object | None = None
    stdin_uncertain: bool = False


class JobManager:
    def __init__(self, max_concurrent=4, capacity=64, retention=300.0,
                 clock=monotonic, journal_path=None, output_archive_path=None,
                 protected_stage=False):
        if (type(max_concurrent) is not int or type(capacity) is not int
                or not 1 <= max_concurrent <= 16 or not max_concurrent <= capacity <= 256):
            raise ValueError("invalid job capacity")
        if (type(retention) not in (int, float) or not math.isfinite(retention)
                or not 0.01 <= retention <= 3600):
            raise ValueError("invalid retention")
        self.max_concurrent, self.capacity = max_concurrent, capacity
        self.retention, self.clock = retention, clock
        self._lock, self._jobs, self._closed = RLock(), {}, False
        self._dev_interactive_requests = {}
        self._stdin_attempts = {}  # Never evict: input IDs are process-local tombstones.
        self._stdin_pending = {}
        # Optional DEV interactive hooks (interactive_hooks.py). None in
        # every release/Broker composition: interactive requests then fail
        # closed in approve_interactive_launch instead of importing DEV code.
        self.interactive_hooks = None
        # In-process replay guard for sessions still retained by this manager.
        # Durable replay prevention across restart belongs to the protected
        # Owner Broker ledger, not to these untrusted client-supplied IDs.
        self._request_sessions = {}
        self._retired_request_ids = set()
        # Retired OS session correlation remains available for trusted
        # Broker slot accounting even after result bytes are pruned.
        self._retired_sessions = {}
        # A failed launch after OS spawn may already have executed its child.
        # Those uncertain attempts cannot be retried within this process.
        # Map of request id -> failure phase (see LaunchOutcomeUnknown);
        # membership and length semantics are the same as the previous set,
        # and the recorded phase is exposed by request_status_readonly.
        self._uncertain_request_ids = {}
        # Only unresolved launch failures enter this bounded process-local set.
        # Store digests, never another copy of potentially secret argv values.
        self._unknown_command_fingerprints = set()
        self._unknown_guard_failed = False
        self.journal_path = journal_path  # Trusted local opt-in; default never writes.
        if type(protected_stage) is not bool:
            raise ValueError("protected_stage must be an explicit boolean")
        self.protected_stage = protected_stage
        if self.protected_stage and os.name != "nt":
            raise ValueError("protected Owner storage requires Windows")
        self._journal_lock = RLock()
        self._previous_sessions = self._load_previous_sessions()
        # Separate opt-in: stdout/stderr may contain secrets. An Owner-private
        # directory and an enabled metadata journal are prerequisites.
        self.output_archive_path = None
        self._archive_lock = RLock()
        self._previous_outputs = {"results": [], "count": 0}
        if output_archive_path is not None:
            from pathlib import Path
            from .offline_result_archive import read_previous_results
            archive = Path(output_archive_path)
            journal = Path(journal_path) if journal_path is not None else None
            if journal is None or archive.parent != journal.parent:
                raise ValueError("output archive needs colocated Owner journal")
            self._previous_outputs = read_previous_results(archive)
            self.output_archive_path = archive

    def _load_previous_sessions(self):
        if self.journal_path is None:
            return {"sessions": [], "count": 0}
        from .session_journal import read_previous_snapshot
        from pathlib import Path
        if not Path(self.journal_path).exists():
            return {"sessions": [], "count": 0}
        return read_previous_snapshot(self.journal_path)

    def previous_sessions(self):
        """Immutable-on-load prior run snapshot; not a live job control surface."""
        with self._lock:
            return {"sessions": [dict(x) for x in self._previous_sessions["sessions"]],
                    "count": self._previous_sessions["count"]}

    def recovery_overview(self, *, include_archive_correlation=False):
        """Inert session guidance; Owner verification belongs to Host.

        Old journal v1 has no request ID. With separately verified Owner
        authorization, an EXISTING result archive supplies its request/session
        relation; never store a new log, decode raw output, or admit work.
        """
        if type(include_archive_correlation) is not bool:
            raise ValueError("invalid archive correlation permission")
        from .recovery_overview import describe_recovery_sessions
        with self._lock:
            current = self.list_sessions()
            previous = self.previous_sessions()
            request_ids = {sid: rid for rid, sid in self._request_sessions.items()}
            archive = []
            if include_archive_correlation and self.output_archive_path is not None:
                archive = [
                    {**{k: item[k] for k in ("request_id", "session_id",
                                               "status", "exit_code", "saved_at")},
                     **({"safe_command": item["safe_command"]}
                        if "safe_command" in item else {})}
                    for item in self._previous_outputs["results"]
                ]
            return describe_recovery_sessions(
                previous, current, journal_enabled=self.journal_path is not None,
                archive_enabled=self.output_archive_path is not None,
                current_request_ids=request_ids, previous_archive_rows=archive)

    def previous_output(self, session_id=None):
        """Read ONLY a prior invocation's saved log; never start a job.

        With no session ID, list bounded metadata and IDs without raw output.
        Individual output is available solely from explicit local opt-in.
        """
        if self.output_archive_path is None:
            return {"status": "DENY", "error": "ArchiveNotEnabled"}
        if session_id is None:
            return {
                "sessions": [
                    {key: value for key, value in row.items()
                     if key not in ("chunks",)}
                    for row in self._previous_outputs["results"]
                ],
                "count": self._previous_outputs["count"],
            }
        if (type(session_id) is not str or len(session_id) != 48
                or any(c not in "0123456789abcdef" for c in session_id)):
            return {"status": "ERROR", "error": "InvalidSessionId"}
        for item in self._previous_outputs["results"]:
            if item["session_id"] == session_id:
                return {**item, "chunks": [dict(c) for c in item["chunks"]]}
        return {"status": "UNKNOWN", "session_id": session_id}

    def previous_output_page(self, request_id, session_id, offset=0):
        """Read a bounded page of a *previous process's* opt-in archive.

        DATA ONLY; NOT an Owner authorization boundary or an MCP/IPC tool.
        Trusted Owner/Broker code MUST authenticate the caller and verify the
        private archive directory's ACL before exposing any output. This
        method does not admit, retry, reconstruct or launch prior jobs.

        Both immutable IDs must match the loaded record. The archive stores
        at most an 8KiB prefix; archived_bytes/end_of_archived distinguish
        surviving data from originally captured bytes after truncation.
        """
        if self.output_archive_path is None:
            return {"status": "DENY", "error": "ArchiveNotEnabled"}
        if (type(request_id) is not str or len(request_id) != 32
                or any(c not in "0123456789abcdef" for c in request_id)
                or type(session_id) is not str or len(session_id) != 48
                or any(c not in "0123456789abcdef" for c in session_id)
                or type(offset) is not int or not 0 <= offset <= 8192):
            return {"status": "ERROR", "error": "InvalidArchivedPage"}
        with self._lock:
            item = next((x for x in self._previous_outputs["results"]
                         if x["request_id"] == request_id
                         and x["session_id"] == session_id), None)
            if item is None:
                return {"request_id": request_id, "status": "UNKNOWN"}
            chunks = item["chunks"]
            # read_previous_results() validates contiguous chunks and strict
            # Base64 before storing a detached Owner-only data snapshot.
            archived = sum(len(base64.b64decode(c["data_base64"], validate=True))
                           for c in chunks)
            if offset > archived:
                return {"status": "ERROR", "error": "OffsetPastArchivedOutput"}
            budget, cursor = 4096, 0
            stdout, stderr = bytearray(), bytearray()
            for chunk in chunks:
                raw = base64.b64decode(chunk["data_base64"], validate=True)
                end = cursor + len(raw)
                if end > offset and budget:
                    fragment = raw[max(0, offset - cursor):][:budget]
                    if chunk["stream"] == "stdout":
                        stdout.extend(fragment)
                    else:
                        stderr.extend(fragment)
                    budget -= len(fragment)
                cursor = end
                if budget == 0:
                    break
            next_offset = offset + len(stdout) + len(stderr)
            return {
                "request_id": request_id, "session_id": session_id,
                "status": item["status"], "exit_code": item["exit_code"],
                "offset": offset, "next_offset": next_offset,
                "captured_bytes": item["captured_bytes"],
                "archived_bytes": archived,
                "end_of_archived": next_offset == archived,
                "archived_truncated": item["archived_truncated"],
                "output_incomplete": item["output_incomplete"],
                "stdout_base64": base64.b64encode(stdout).decode("ascii"),
                "stderr_base64": base64.b64encode(stderr).decode("ascii"),
            }

    def _snapshot_result_archive(self, job):
        """Checkpoint a bounded prefix of output; never grant/replay commands.

        Copy under the job lock and release it BEFORE acquiring archive lock.
        The whole archive write is atomic; unexpected crash leaves the last
        fully persisted checkpoint or none, never an invented completion.
        """
        if self.output_archive_path is None or job.request.interactive:
            return
        from .offline_result_archive import MAX_CAPTURE, write_result_checkpoint
        import base64
        from time import time
        with job.lock:
            # The archive format allows at most 128 chunks. Programs can
            # write thousands of tiny fragments even below the 8KiB budget:
            # coalesce adjacent same-stream fragments, and keep a bounded
            # prefix when stdout/stderr alternates too often. Otherwise a
            # late checkpoint would fail validation and lose the exit code.
            grouped, archived = [], 0
            for start, stream, raw in job.chunks:
                if archived >= MAX_CAPTURE:
                    break
                data = raw[:MAX_CAPTURE - archived]
                if not data:
                    continue
                if grouped and grouped[-1][0] == stream:
                    grouped[-1][1].extend(data)
                elif len(grouped) < 128:
                    grouped.append((stream, bytearray(data)))
                else:
                    break
                archived += len(data)
            pieces = []
            offset = 0
            for stream, data in grouped:
                pieces.append({
                    "offset": offset, "stream": stream,
                    "data_base64": base64.b64encode(data).decode("ascii"),
                })
                offset += len(data)
            from .safe_command_identity import safe_command_identity
            safe_command = safe_command_identity(
                job.request.executable, job.request.argv, job.request.cwd)
            entry = {
                "request_id": job.request.request_id,
                "session_id": job.session_id,
                "status": job.status,
                "exit_code": job.exit_code,
                "captured_bytes": job.captured,
                "output_incomplete": job.output_incomplete or job.overflow,
                "archived_truncated": job.captured > archived,
                "saved_at": int(time()),
                "chunks": pieces,
            }
            if safe_command is not None:
                entry["safe_command"] = safe_command
        with self._archive_lock:
            write_result_checkpoint(self.output_archive_path, entry,
                                    protected_stage=self.protected_stage)

    def _snapshot_journal(self):
        if self.journal_path is None:
            return
        from .session_journal import write_snapshot
        # START holds _lock before entering this method, while the supervisor
        # calls it independently. Keep the SAME lock order for both paths:
        # manager -> journal. The former journal -> manager order could
        # deadlock when a job finished while another was being admitted.
        with self._lock:
            with self._journal_lock:
                write_snapshot(self.journal_path, self.list_sessions()["sessions"],
                               protected_stage=self.protected_stage)

    def _prune(self):
        for key, job in list(self._jobs.items()):
            if (job.done.is_set() and not job.thread.is_alive() and job.child.poll() is not None
                    and self.clock() >= job.finished_at + self.retention):
                # Preserve the spent ID even when result storage is evicted.
                # Otherwise reconnect/re-submit after retention runs it again.
                self._retired_request_ids.add(job.request.request_id)
                self._retired_sessions[job.request.request_id] = job.session_id
                del self._jobs[key]
                self._request_sessions.pop(job.request.request_id, None)

    def blocked_unknown_command_readonly(self, request: Command):
        """Preflight cross-ID ambiguity only; start() remains authoritative."""
        if not isinstance(request, Command):
            raise TypeError("validated Command required")
        with self._lock:
            # Preserve the existing same-ID uncertainty and active-session paths.
            if (request.request_id in self._uncertain_request_ids
                    or request.request_id in self._request_sessions
                    or request.request_id in self._retired_request_ids):
                return False
            if self._unknown_guard_failed:
                return "GUARD_UNAVAILABLE"
            return (_unknown_command_fingerprint(request)
                    in self._unknown_command_fingerprints)

    def start(self, request: Command):
        if not isinstance(request, Command):
            raise TypeError("validated Command required")
        if (request.interactive and (self.protected_stage or self.journal_path is not None
                                    or self.output_archive_path is not None)
                or getattr(self, "_dev_interactive_only", False) and not request.interactive):
            raise PermissionError("request manager mismatch")
        with self._lock:
            self._prune()
            if self._closed:
                raise RuntimeError("job manager closed")
            if request.request_id in self._retired_request_ids:
                return {"request_id": request.request_id,
                        "status": "DENY_DUPLICATE_EXPIRED",
                        "error": "RequestIdRetired"}
            if request.request_id in self._uncertain_request_ids:
                return {"request_id": request.request_id,
                        "status": "DENY_DUPLICATE_UNKNOWN",
                        "error": "LaunchOutcomeUnknown"}
            original_id = self._request_sessions.get(request.request_id)
            if original_id is not None:
                original = self._jobs.get(original_id)
                if original is None:
                    raise RuntimeError("inconsistent request session index")
                if original.request != request:
                    return {"request_id": request.request_id,
                            "status": "DENY_DUPLICATE_CONFLICT",
                            "error": "RequestIdConflict"}
                return {"request_id": request.request_id,
                        "status": "RUNNING", "session_id": original_id,
                        "reused_session": True}
            if self._unknown_guard_failed:
                return {"request_id": request.request_id,
                        "status": "DENY_REPLAY_GUARD_FULL",
                        "error": "ProtectedBrokerLedgerRequired"}
            fingerprint = _unknown_command_fingerprint(request)
            if fingerprint in self._unknown_command_fingerprints:
                return {"request_id": request.request_id,
                        "status": "DENY_DUPLICATE_UNKNOWN",
                        "error": "PossibleDuplicateLaunchOutcomeUnknown"}
            # Never evict a tombstone to make room: that would permit replay.
            # Only NEW admissions are held; existing sessions stay queryable.
            # A protected Broker ledger is needed before this guard fills.
            if (len(self._retired_request_ids) + len(self._uncertain_request_ids)
                    >= _MAX_RETIRED_REQUESTS
                    or len(self._unknown_command_fingerprints) >= _MAX_RETIRED_REQUESTS):
                return {"request_id": request.request_id,
                        "status": "DENY_REPLAY_GUARD_FULL",
                        "error": "ProtectedBrokerLedgerRequired"}
            if len(self._jobs) >= self.capacity:
                return {"status": "BUSY", "error": "SessionCapacity"}
            if sum(j.thread.is_alive() or j.child.poll() is None for j in self._jobs.values()) >= self.max_concurrent:
                return {"status": "BUSY", "error": "ConcurrencyLimit"}
            interactive_guard = None
            if request.interactive:
                interactive_guard = approve_interactive_launch(self, request)
            # Reserve before the first OS launch side effect. A failed Popen,
            # Job assignment or supervision-start is UNKNOWN, not fresh.
            # If the request starts successfully, the active session index
            # supersedes this tentative marker; _prune later retires it.
            # The marker value is the failure phase once known; it is only
            # read after a failed attempt (see request_status_readonly).
            self._uncertain_request_ids[request.request_id] = "pre_spawn"
            # On Windows the child must not execute before Job Object admission.
            tree = None
            child = None
            job = None
            phase = "pre_spawn"
            try:
                tree = WindowsJob() if os.name == "nt" else None
                phase = "spawning"
                child = subprocess.Popen(
                    [request.executable, *request.argv], cwd=request.cwd,
                    shell=False, stdin=subprocess.PIPE if request.interactive else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    bufsize=0, close_fds=True, env=_child_environment(),
                    creationflags=0x00000004 if tree is not None else 0,
                )  # CREATE_SUSPENDED (Windows)
                if request.interactive:
                    # Fail closed before resume if this Python/Windows cannot
                    # provide nonblocking writes to the anonymous stdin pipe.
                    os.set_blocking(child.stdin.fileno(), False)
                if tree is None:
                    for pipe in (child.stdout, child.stderr):
                        os.set_blocking(pipe.fileno(), False)
                    # No suspension off Windows: the child is executing now.
                    phase = "spawned_running"
                else:
                    # CREATE_SUSPENDED: the primary thread has not run yet.
                    phase = "spawned_suspended"
                # On Windows the process remains CREATE_SUSPENDED until all
                # opted-in initial checkpoints have been durably written.
                job = _Job(secrets.token_hex(24), request, child,
                           monotonic() + request.timeout, tree=tree)
                job.stdin_owner = interactive_guard
                job.thread = Thread(target=self._supervise, args=(job,), daemon=True,
                                    name="sora-job-" + job.session_id[:8])
                self._jobs[job.session_id] = job
                if self.journal_path is not None:
                    # Persist RUNNING before launching the supervisor thread:
                    # a failed initial write must not race finalization.
                    self._snapshot_journal()
                # Opt-in output archive starts with a zero-byte RUNNING
                # checkpoint. If it cannot be stored, fail closed before the
                # suspended child is allowed to proceed with supervision.
                if self.output_archive_path is not None:
                    self._snapshot_result_archive(job)
                if tree is not None:
                    # Never execute an unrecorded child: resume only after
                    # required journal/archive initialization succeeds.
                    phase = "resuming"
                    tree.attach_and_resume(child)
                    phase = "resumed"
                job.thread.start()
                phase = "supervised"
                self._request_sessions[request.request_id] = job.session_id
                self._uncertain_request_ids.pop(request.request_id, None)
            except (LaunchOutcomeUnknown, LaunchNotStarted):
                raise
            except Exception as exc:
                if job is not None:
                    self._jobs.pop(job.session_id, None)
                may_have_executed = phase not in ("pre_spawn", "spawned_suspended")
                if may_have_executed:
                    # Persist both process-local replay guards BEFORE cleanup;
                    # cleanup may itself fail or throw. Never lose UNKNOWN.
                    self._uncertain_request_ids[request.request_id] = phase
                    try:
                        self._unknown_command_fingerprints.add(fingerprint)
                    except Exception:
                        # Any fingerprint storage failure closes new admissions.
                        self._unknown_guard_failed = True
                try:
                    cleanup_failed = _discard_spawn(child, tree) is not True
                except Exception:
                    cleanup_failed = True
                if cleanup_failed:
                    # Failed cleanup cannot establish a safe retry, even
                    # when a suspended child has not yet been resumed.
                    self._unknown_guard_failed = True
                if not may_have_executed and not cleanup_failed:
                    # Before spawn or CREATE_SUSPENDED, execution did not start.
                    # Approval consumption is independent from this classification.
                    self._uncertain_request_ids.pop(request.request_id, None)
                    raise LaunchNotStarted(
                        request.request_id, phase,
                        "launch failed before the child could run") from exc
                if not may_have_executed:
                    # Cleanup failure prevents safe classification even when
                    # startup had not yet resumed the child.
                    self._uncertain_request_ids[request.request_id] = phase
                raise LaunchOutcomeUnknown(
                    request.request_id, phase,
                    "launch outcome unknown; command may have executed") from exc
            return {"request_id": request.request_id, "status": "RUNNING",
                    "session_id": job.session_id}

    def _supervise(self, job):
        child, state = job.child, None
        pipes = {"stdout": child.stdout, "stderr": child.stderr}
        drain_deadline = None
        tree_terminated = False
        pause = Event()
        try:
            while True:
                output_changed = False
                code = child.poll()
                if code is None and state is None:
                    if job.cancel.is_set():
                        state = "CANCELLED"
                    elif monotonic() >= job.deadline:
                        state = "TIMEOUT"
                for stream, pipe in list(pipes.items()):
                    chunk = _read_available(pipe)
                    if chunk == b"":
                        del pipes[stream]
                    elif chunk:
                        with job.lock:
                            remaining = job.request.output_limit - job.captured
                            data = chunk[:remaining]
                            if data:
                                job.chunks.append((job.captured, stream, data))
                                job.captured += len(data)
                                output_changed = True
                            if len(chunk) > remaining:
                                job.overflow = True
                                state = state or "OUTPUT_LIMIT"
                if output_changed and self.output_archive_path is not None:
                    try:
                        self._snapshot_result_archive(job)
                    except (OSError, ValueError):
                        # Preserve the last complete disk checkpoint; running
                        # process outcome must not be invented or relaunched.
                        pass
                # On cancel/timeout/overflow, or when the direct parent exits,
                # terminate the owned tree, not only its first process.
                if (job.tree is not None and not tree_terminated
                        and (state is not None or child.poll() is not None)):
                    job.tree.terminate()
                    tree_terminated = True
                if state is not None and child.poll() is None and job.tree is None:
                    child.kill()
                code = child.poll()
                if code is not None:
                    drain_deadline = drain_deadline or monotonic() + 0.25
                    if not pipes:
                        break
                    if monotonic() >= drain_deadline:
                        job.output_incomplete = True
                        state = state or "OUTPUT_INCOMPLETE"
                        break
                if state is not None:
                    drain_deadline = drain_deadline or monotonic() + 0.5
                    if monotonic() >= drain_deadline:
                        job.output_incomplete = True
                        state = "OUTCOME_UNKNOWN"
                        break
                pause.wait(0.005)
            state = state or ("COMPLETED" if child.returncode == 0 else "FAILED")
        except Exception:
            state = "ERROR"
            job.output_incomplete = True
            try:
                if job.tree is not None:
                    job.tree.terminate()
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=0.5)
            except Exception:
                state = "OUTCOME_UNKNOWN"
        finally:
            if job.tree is not None:
                try:
                    job.tree.close()
                except OSError:
                    state = "OUTCOME_UNKNOWN"
                    job.output_incomplete = True
            for pipe in (getattr(child, "stdin", None), child.stdout, child.stderr):
                if pipe is None:
                    continue
                try:
                    pipe.close()
                except OSError:
                    state = "ERROR"
                    job.output_incomplete = True
            try:
                code = child.poll()
            except OSError:
                code, state = None, "OUTCOME_UNKNOWN"
            with job.lock:
                job.status, job.exit_code = state, code
                job.finished_at = self.clock()
                job.done.set()
            if self.output_archive_path is not None:
                try:
                    # Persist final status/exit only when actually observed.
                    self._snapshot_result_archive(job)
                except (OSError, ValueError):
                    # Last durable RUNNING entry becomes INTERRUPTED_UNKNOWN
                    # on manual recovery. No automated retry.
                    pass
            if self.journal_path is not None:
                try:
                    self._snapshot_journal()
                except (OSError, ValueError):
                    # Job outcome is independent of diagnostic journal I/O.
                    # The previous RUNNING snapshot remains UNKNOWN on restart.
                    pass

    def _lookup(self, session_id):
        if not isinstance(session_id, str) or len(session_id) != 48 or any(c not in "0123456789abcdef" for c in session_id):
            raise ValueError("invalid session ID")
        self._prune()
        if session_id not in self._jobs:
            raise UnknownSession("unknown or expired session")
        return self._jobs[session_id]

    def confirm_terminal_readonly(self, request_id, session_id):
        """Confirm a specific child has exited AND its supervisor finalized.

        For internal trusted Broker slot accounting only. Do NOT expose this
        as client authorization, external IPC, or a command replay decision.
        Failure, missing sessions or uncertain outcomes keep the slot held.
        """
        if (type(request_id) is not str or not request_id
                or type(session_id) is not str or len(session_id) != 48
                or any(c not in "0123456789abcdef" for c in session_id)):
            return False
        with self._lock:
            job = self._jobs.get(session_id)
            if job is None:
                # _prune retires only supervisor-finalized, exited jobs.
                # Its in-process tombstone remains even after output expires.
                return (request_id in self._retired_request_ids
                        and self._retired_sessions.get(request_id) == session_id)
            if job.request.request_id != request_id:
                return False
            with job.lock:
                if not job.done.is_set() or job.status not in (
                        "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
                        "OUTPUT_LIMIT", "OUTPUT_INCOMPLETE", "ERROR"):
                    return False
                try:
                    return job.child.poll() is not None
                except OSError:
                    return False

    def request_status_readonly(self, request_id):
        """Bounded metadata-only lookup; never admits or replays a job.

        Request IDs are correlation labels, not admission tokens. Retained
        sessions only; the future protected Broker owns durable request state.
        """
        if type(request_id) is not str or not request_id:
            return {"status": "ERROR", "error": "InvalidRequestId"}
        with self._lock:
            session_id = self._request_sessions.get(request_id)
            if session_id is None:
                if request_id in self._uncertain_request_ids:
                    return {"request_id": request_id, "status": "OUTCOME_UNKNOWN",
                            "launch_phase": self._uncertain_request_ids[request_id]}
                # A retired id reports OUTCOME_UNKNOWN too: its outcome
                # record expired, so this manager can no longer state it.
                # Observing an expired job does not imply it never ran;
                # the resend reply (DENY_DUPLICATE_EXPIRED) is what
                # distinguishes it from an uncertain launch.
                return {"request_id": request_id,
                        "status": ("OUTCOME_UNKNOWN" if
                                   request_id in self._retired_request_ids
                                   else "UNKNOWN")}
            job = self._jobs.get(session_id)
            if job is None:
                return {"request_id": request_id, "status": "UNKNOWN"}
            with job.lock:
                return {
                    "request_id": request_id,
                    "session_id": session_id,
                    "status": job.status,
                    "exit_code": job.exit_code,
                    "captured_bytes": job.captured,
                    "output_incomplete": job.output_incomplete,
                }

    def read_live_page(self, session_id, offset=0):
        """Read at most 4096 raw captured bytes from one retained live job.

        Trusted in-process data API only; no authentication or job admission.
        Offsets count observed capture bytes across both streams in chunk
        order. end_of_capture marks the snapshot's tail, even while RUNNING;
        a later read at next_offset may therefore yield additional bytes.
        """
        if type(offset) is not int or not 0 <= offset <= 65536:
            raise ValueError("invalid byte offset")
        with self._lock:
            job = self._lookup(session_id)
        with job.lock:
            if offset > job.captured:
                raise ValueError("offset past captured output")
            end = min(job.captured, offset + 4096)
            stdout, stderr = bytearray(), bytearray()
            for start, stream, raw in job.chunks:
                if start >= end:
                    break
                if start + len(raw) <= offset:
                    continue
                fragment = raw[max(0, offset - start):end - start]
                if stream == "stdout":
                    stdout.extend(fragment)
                else:
                    stderr.extend(fragment)
            return {
                "request_id": job.request.request_id, "session_id": session_id,
                "status": job.status, "exit_code": job.exit_code,
                "offset": offset, "next_offset": end,
                "captured_bytes": job.captured,
                "output_incomplete": job.output_incomplete,
                "overflow": job.overflow, "end_of_capture": end == job.captured,
                "stdout_base64": base64.b64encode(stdout).decode("ascii"),
                "stderr_base64": base64.b64encode(stderr).decode("ascii"),
            }

    def read(self, session_id, offset=0):
        with self._lock:
            job = self._lookup(session_id)
        with job.lock:
            if type(offset) is not int or not 0 <= offset <= job.captured:
                raise ValueError("invalid byte offset")
            selected = [(max(start, offset), stream, data[max(0, offset - start):])
                        for start, stream, data in job.chunks if start + len(data) > offset]
            out = b"".join(data for _, stream, data in selected if stream == "stdout")
            err = b"".join(data for _, stream, data in selected if stream == "stderr")
            return {"request_id": job.request.request_id, "session_id": session_id,
                    "status": job.status, "pid": job.child.pid, "exit_code": job.exit_code,
                    "stdout": out.decode("utf-8", errors="replace"),
                    "stderr": err.decode("utf-8", errors="replace"),
                    "offset": offset, "next_offset": job.captured,
                    "captured_bytes": job.captured, "returned_bytes": len(out) + len(err),
                    "truncated": job.overflow, "overflow": job.overflow,
                    "output_incomplete": job.output_incomplete,
                    "chunks": [{"offset": start, "stream": stream,
                                "data_base64": base64.b64encode(data).decode("ascii")}
                               for start, stream, data in selected]}

    def list_sessions(self):
        """Bounded metadata-only snapshot; never expose command argv or env."""
        with self._lock:
            self._prune()
            jobs = list(self._jobs.values())
        items = []
        for job in jobs:
            with job.lock:
                items.append({"session_id": job.session_id,
                              "status": job.status, "pid": job.child.pid,
                              "exit_code": job.exit_code,
                              "captured_bytes": job.captured,
                              "output_incomplete": job.output_incomplete})
        return {"sessions": items, "count": len(items)}

    def stop(self, session_id):
        with self._lock:
            job = self._lookup(session_id)
        with job.lock:
            if not job.done.is_set():
                job.cancel.set()
                return {"session_id": session_id, "status": "CANCELLING"}
            return {"session_id": session_id, "status": job.status, "already_finished": True}

    def wait(self, session_id):
        with self._lock:
            job = self._lookup(session_id)
        if not job.done.wait(job.request.timeout + 2):
            return {"session_id": session_id, "status": "OUTCOME_UNKNOWN"}
        job.thread.join(timeout=0.1)
        return self.read(session_id)

    def wait_for(self, session_id, wait_seconds):
        """Read-only bounded completion wait; never launches or retries a job.

        A still-running job returns a normal RUNNING snapshot, preserving the
        same session_id for a subsequent read/wait without duplicate dispatch.
        """
        if (type(wait_seconds) not in (int, float)
                or not math.isfinite(wait_seconds)
                or not 0 <= wait_seconds <= 90):
            raise ValueError("invalid bounded wait")
        with self._lock:
            job = self._lookup(session_id)
        if job.done.wait(wait_seconds):
            job.thread.join(timeout=0.1)
        return self.read(session_id)

    def close(self):
        """Explicit local teardown, never invoked by a transport disconnect."""
        with self._lock:
            self._closed = True
            jobs = list(self._jobs.values())
            for job in jobs:
                job.cancel.set()
        deadline = monotonic() + 1
        for job in jobs:
            job.thread.join(timeout=max(0, deadline - monotonic()))
