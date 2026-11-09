"""DEV ONLY: single-process Owner review authority hosted independently.

The in-process Host is a private implementation detail of the Owner process.
It is NOT registered as MCP/HTTP, cannot accept remote input and has exactly
one fixed local REVIEW command. The only launch site is the existing
WindowsOwnerController.approve_local after actual OwnerApprovalBroker UI proof.

Optional DEV-only SQLite launch-attempt receipts are a deny-only replay fence;
no consent claim or database row authorizes launch. This is NOT BrokerRuntime
v2, code-provenance protection, or live IPC cutover.
"""
from threading import RLock

from .approval_v2_ledger import OfflineApprovalLedger
from .commands import Command
from .host import Host
from .jobs import JobManager
from .owner_approval_broker import OwnerApprovalBroker
from .policy import Policy
from .windows_owner import OwnerSessionGuard
from .windows_owner_controller import WindowsOwnerController


class _DevDurableLaunchFencedJobs:
    """Extra write-ahead REPLAY fence, never a source of launch permission.

    Used only by a private Owner GUI Host with no AUTO or SCOPED policy.
    Existing OwnerApprovalBroker->WindowsOwnerController still makes every
    approval decision. A data-only SQLite row can DENY but never APPROVE.
    """

    def __init__(self, *, jobs, ledger, fixed, guard):
        self._jobs, self._ledger = jobs, ledger
        self._fixed, self._guard = fixed, guard

    def start(self, command):
        if type(command) is not Command or command != self._fixed:
            raise PermissionError("unbound private Owner command")
        self._guard.verify()
        receipt = self._ledger.reserve_launch_attempt_data_only(command)
        if receipt.status != "ATTEMPT_RECORDED_DATA_ONLY":
            raise PermissionError("durable request ID already attempted")
        self._guard.verify()
        # If the OS may have launched a child, never roll back the SQLite
        # receipt on exception, missing ACK, failed supervision or disconnect.
        return self._jobs.start(command)

    def __getattr__(self, name):
        # Host invokes start() via this wrapper; status/read/close still go to
        # its ONE real JobManager. This is NOT an isolation boundary.
        return getattr(self._jobs, name)


class DevOwnerAuthority:
    """One-use local Owner gate and JobManager, owned by one OS process."""

    def __init__(self, *, fixed_command, offline_ledger=None):
        if type(fixed_command) is not Command:
            raise TypeError("immutable fixed local Command required")
        if (len(fixed_command.request_id) != 32
                or any(x not in "0123456789abcdef" for x in fixed_command.request_id)):
            raise ValueError("fixed request ID must be opaque")
        self._guard = OwnerSessionGuard()
        self._guard.verify()
        if offline_ledger is not None and type(offline_ledger) is not OfflineApprovalLedger:
            raise TypeError("explicit DEV-only SQLite ledger required")
        self._ledger = offline_ledger
        self._jobs = JobManager(max_concurrent=1, capacity=2, retention=30.0)
        owned_jobs = (
            _DevDurableLaunchFencedJobs(
                jobs=self._jobs, ledger=offline_ledger,
                fixed=fixed_command, guard=self._guard)
            if offline_ledger is not None else self._jobs)
        self._host = Host(
            policy=Policy(roots=(fixed_command.cwd,), auto=()),
            owner=self._guard.sid, jobs=owned_jobs)
        self._controller = WindowsOwnerController(self._host)
        self._reviewer = OwnerApprovalBroker(self._controller)
        self._fixed = fixed_command
        self._pending = None
        self._reviewed = False
        self._session = None
        self._closed = False
        self._lock = RLock()

    def stage_local(self):
        """Stage exactly one REVIEW; never launches or shows a GUI."""
        self._guard.verify()
        with self._lock:
            if self._closed or self._pending is not None:
                raise PermissionError("Owner request already staged or unavailable")
            # A replay from a prior DEV process must not trigger another UI,
            # let alone a launch. Database status is DENY-only evidence, NOT
            # permission even when it says NOT_ATTEMPTED_DATA_ONLY.
            req = self._fixed
            if self._ledger is not None and (
                    self._ledger.inspect_launch_attempt_data_only(req).status
                    != "NOT_ATTEMPTED_DATA_ONLY"):
                raise PermissionError("previous launch attempt must not replay")
            result = self._host.run_command(
                background=True, executable=req.executable,
                argv=req.argv, cwd=req.cwd, timeout=req.timeout,
                output_limit=req.output_limit, request_id=req.request_id)
            if result.get("status") != "REVIEW":
                raise PermissionError("strict local REVIEW not available")
            self._pending = result["pending_id"]
            return self._pending

    def review_once_local(self):
        """Requires real local GUI; one-use Owner decision remains in-process."""
        self._guard.verify()
        with self._lock:
            if self._closed or self._pending is None or self._reviewed:
                raise PermissionError("local Owner review unavailable")
            self._reviewed = True  # consume even if UI, lock or admission fails
            pending_id = self._pending
        result = self._reviewer.review_once(pending_id)
        self._guard.verify()
        with self._lock:
            if (result.get("status") == "RUNNING"
                    and type(result.get("session_id")) is str
                    and len(result["session_id"]) == 48
                    and all(c in "0123456789abcdef" for c in result["session_id"])):
                # Verify that the record belongs to this process's exact
                # immutable fixed request, not an unrelated session ID.
                status = self._jobs.request_status_readonly(self._fixed.request_id)
                if status.get("session_id") != result["session_id"]:
                    raise PermissionError("unverifiable local session")
                self._session = result["session_id"]
                return "RUNNING"
            if result.get("status") == "DENIED":
                return "DENIED"
            return "OUTCOME_UNKNOWN"  # ambiguous launch is never replayed

    def verify_local(self):
        """Check that the original Owner still owns an open DEV process.

        No permission or session token is returned. Call after any lengthy
        result formatting too, since the Owner can lock during serialization.
        """
        self._guard.verify()
        with self._lock:
            if self._closed:
                raise PermissionError("Owner authority closed")
        self._guard.verify()

    def _verified_owned_session_locked(self, request_id, session_id):
        """Never let an alleged request/session ID become Owner authority."""
        if (self._closed or self._session is None
                or type(request_id) is not str
                or request_id != self._fixed.request_id
                or type(session_id) is not str
                or session_id != self._session):
            raise LookupError("unknown or unapproved Owner job")
        known = self._jobs.request_status_readonly(request_id)
        if (type(known) is not dict
                or known.get("request_id") != request_id
                or known.get("session_id") != session_id):
            raise LookupError("owned job no longer correlated")
        return known

    def status_verified_local(self, request_id, session_id):
        """Metadata-only status from the already-owned, approved job.

        No stdout/stderr, argv, pid, environment, admission or new process.
        Returns no data from unrelated JobManager sessions.
        """
        self._guard.verify()
        with self._lock:
            known = self._verified_owned_session_locked(request_id, session_id)
            allowed = {"RUNNING", "COMPLETED", "FAILED", "CANCELLED",
                       "TIMEOUT", "OUTPUT_LIMIT", "OUTPUT_INCOMPLETE",
                       "ERROR", "OUTCOME_UNKNOWN", "INTERRUPTED_UNKNOWN"}
            if (type(known.get("status")) is not str
                    or known["status"] not in allowed
                    or (known.get("exit_code") is not None and
                        type(known["exit_code"]) is not int)
                    or type(known.get("captured_bytes")) is not int
                    or not 0 <= known["captured_bytes"] <= self._fixed.output_limit
                    or type(known.get("output_incomplete")) is not bool):
                raise LookupError("invalid Owner job metadata")
            result = {
                "request_id": request_id, "session_id": session_id,
                "status": known["status"], "exit_code": known["exit_code"],
                "captured_bytes": known["captured_bytes"],
                "output_incomplete": known["output_incomplete"],
            }
            self._guard.verify()
            return result

    def read_verified_local(self, request_id, session_id, offset):
        """Read one private snapshot from exactly ONE approved Owner job.

        Read-only. The verified request/session pair is checked against the
        original local GUI-approved fixed request before JobManager access.
        """
        self._guard.verify()
        with self._lock:
            self._verified_owned_session_locked(request_id, session_id)
            if (type(offset) is not int
                    or not 0 <= offset <= self._fixed.output_limit):
                raise LookupError("invalid Owner result offset")
            try:
                result = self._jobs.read(session_id, offset)
            except (LookupError, ValueError) as exc:
                raise LookupError("owned job result unavailable") from exc
            self._guard.verify()
            if (type(result) is not dict
                    or result.get("request_id") != request_id
                    or result.get("session_id") != session_id
                    or result.get("offset") != offset):
                raise LookupError("owned job result mismatch")
            return result

    def wait_local(self, seconds=5.0):
        """Owner-local observation only; never retries or starts a job."""
        self._guard.verify()
        with self._lock:
            if self._closed or self._session is None:
                raise PermissionError("no verified approved local job")
            sid = self._session
        result = self._jobs.wait_for(sid, seconds)
        self._guard.verify()
        if (type(result) is not dict
                or result.get("session_id") != sid
                or result.get("request_id") != self._fixed.request_id):
            raise PermissionError("Owner job correlation unknown")
        if self._ledger is not None and result.get("status") in (
                "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
                "OUTPUT_LIMIT", "OUTPUT_INCOMPLETE", "ERROR"):
            # This marks evidence of a terminal result, NOT a grant or safe
            # permission to re-execute. Ambiguous results remain UNKNOWN.
            self._ledger.conclude_launch_attempt_data_only(self._fixed)
        return result

    def close(self):
        with self._lock:
            self._closed = True
            self._jobs.close()
