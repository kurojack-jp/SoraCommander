"""Explicit, in-process Owner Broker lifetime; no listener or startup execution.

Only trusted local configuration belongs here. Paths are checked for explicit
local fixed-drive files/directories, not publisher identity or protected trust.
HOLD_LIVE: protected code provenance/ACLs, durable replay ledger, real Owner GUI
and SID/LUID/WTS-authenticated IPC remain unaccepted. Request IDs authenticate
nobody. The private runtime is reserved for future trusted Owner orchestration.

All operations, including teardown, run on the acquiring OS thread. A failed
manager close retains the lease and disables reads permanently; external Owner
recovery/process termination may be needed. Uncertain launches or pruned job
evidence also retain it: this manager cannot attest retired tree cleanup. A
bounded close that leaves workers running retains the lease; a later same-thread
close may confirm their exit.
There is no destructor, automatic retry, process loop or activation endpoint.
"""
import ctypes
from ctypes import wintypes
import ntpath
from pathlib import Path
import re
import stat
import threading
import time

from .broker_owner_lease import BrokerOwnerLease
from .broker_runtime import BrokerRuntime
from .jobs import JobManager
from .windows_owner import OwnerSessionGuard


_GetDriveTypeW = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
_GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
_GetDriveTypeW.restype = wintypes.UINT
_STATUSES = frozenset(("RUNNING", "COMPLETED", "FAILED", "CANCELLING",
                       "CANCELLED", "TIMEOUT", "OUTPUT_LIMIT",
                       "OUTPUT_INCOMPLETE", "ERROR", "OUTCOME_UNKNOWN"))


class BrokerOwnerProcessError(PermissionError):
    """Inactive Owner context or unconfirmed teardown; never continue authority."""


def _local_path(value, *, directory):
    # Do not resolve/normalize a remote, device, relative or ambiguous alias.
    if (type(value) is not str or not 0 < len(value) <= 8192
            or any(ord(c) < 32 for c in value)):
        raise ValueError("explicit local configuration path required")
    drive, tail = ntpath.splitdrive(value)
    if (re.fullmatch(r"[A-Za-z]:", drive) is None
            or not tail.startswith(("\\", "/"))):
        raise ValueError("absolute local drive path required")
    parts = tail.replace("/", "\\").split("\\")[1:]
    if parts and parts[-1] == "":
        parts.pop()
    if any(not p or p in (".", "..") or p.endswith((".", " "))
           or any(c in p for c in ':*?"<>|')
           or re.fullmatch(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]",
                           p.split(".")[0], re.IGNORECASE)
           for p in parts):
        raise ValueError("ambiguous configuration path")
    if _GetDriveTypeW(drive + "\\") != 3:  # DRIVE_FIXED; denies mapped drives.
        raise ValueError("local fixed drive required")
    path = Path(value)
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("configuration reparse path denied")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("configuration target type mismatch")
    return value


class BrokerOwnerProcess:
    """Single-use lifecycle context, inert until explicit context entry.

    Construction validates local inputs only. It creates no guard, lease,
    JobManager, runtime, OS child, worker, approval or Windows Job Object.
    This Python object is not an isolation boundary against same-process code.
    """

    def __init__(self, expected_executable, expected_cwd, *, clock=time.monotonic,
                 capacity=4):
        self._expected_executable = _local_path(expected_executable, directory=False)
        self._expected_cwd = _local_path(expected_cwd, directory=True)
        if type(capacity) is not int or not 1 <= capacity <= 16 or not callable(clock):
            raise ValueError("invalid capacity/clock")
        self._clock, self._capacity = clock, capacity
        self._lease = self._guard = self._identity = None
        self._manager = self._runtime = None
        self._thread_id = None
        self._attempted = self._active = self._closing = self._closed = False
        self._manager_close_attempted = self._manager_close_failed = False

    def _same_thread(self):
        if self._thread_id != threading.get_native_id():
            raise BrokerOwnerProcessError("acquiring OS thread required")

    def _verify_owner(self):
        if (self._lease is None or self._lease._handle is None
                or self._lease._guard is not self._guard
                or type(self._guard) is not OwnerSessionGuard
                or self._lease._identity != self._identity
                or self._guard._identity != self._identity):
            raise BrokerOwnerProcessError("pinned Owner lease required")
        self._lease._verify()

    def _require_active(self):
        self._same_thread()
        if not self._active or self._closing or self._closed:
            raise BrokerOwnerProcessError("Owner Broker context inactive")
        self._verify_owner()

    def __enter__(self):
        if self._attempted or self._closed:
            raise BrokerOwnerProcessError("Owner Broker context is single-use")
        self._attempted = True
        self._thread_id = threading.get_native_id()
        try:
            # Recheck paths in case local config targets changed since construction.
            _local_path(self._expected_executable, directory=False)
            _local_path(self._expected_cwd, directory=True)
            self._lease = BrokerOwnerLease()
            self._lease.__enter__()
            self._guard = self._lease._guard
            self._identity = self._lease._identity
            self._verify_owner()
            self._manager = JobManager(max_concurrent=self._capacity)
            self._verify_owner()
            self._runtime = BrokerRuntime(
                expected_executable=self._expected_executable,
                expected_cwd=self._expected_cwd, owner_binding=self._identity.sid,
                owner_guard=self._guard, clock=self._clock, capacity=self._capacity,
                manager=self._manager)
            self._verify_owner()
            self._active = True
            return self
        except BaseException:
            self.close()
            raise

    def read_result_page_wire_local(self, raw):
        """Owner-local bounded read; never a remote endpoint/client authenticator."""
        self._require_active()
        try:
            result = self._runtime.read_result_page_wire_local(raw)
        finally:
            self._require_active()
        return result

    def query_status_local(self, request_id):
        """Exact retained request/session metadata only, never output/argv/paths."""
        self._require_active()
        try:
            if type(request_id) is not str or re.fullmatch("[0-9a-f]{32}", request_id) is None:
                raise ValueError("invalid status lookup")
            runtime = self._runtime
            with runtime._lock:
                session = runtime.sessions.get(request_id)
                response = {"request_id": request_id, "status": "UNKNOWN"}
                if session is not None:
                    response["status"] = "OUTCOME_UNKNOWN"
                    if type(session) is str and re.fullmatch("[0-9a-f]{48}", session):
                        try:
                            snapshot = self._manager.request_status_readonly(request_id)
                        except Exception:
                            snapshot = None
                        if (type(snapshot) is dict
                                and type(snapshot.get("request_id")) is str
                                and snapshot["request_id"] == request_id
                                and type(snapshot.get("session_id")) is str
                                and snapshot["session_id"] == session
                                and runtime.sessions.get(request_id) == session
                                and type(snapshot.get("status")) is str
                                and snapshot["status"] in _STATUSES):
                            response["status"] = snapshot["status"]
                return response
        finally:
            self._require_active()

    def _confirm_shutdown(self):
        # JobManager.close only signals cancellation and joins for one second.
        # Inspect its retained ownership evidence, not client-supplied IDs or
        # status alone. Uncertain spawn/Job Object close cannot release authority.
        with self._manager._lock:
            if (self._manager._closed is not True
                    or self._manager._uncertain_request_ids
                    or self._manager._retired_request_ids
                    or self._manager._retired_sessions):
                raise BrokerOwnerProcessError("manager shutdown unconfirmed; lease retained")
            for job in self._manager._jobs.values():
                if (job.thread is None or job.thread.is_alive()
                        or not job.done.is_set() or type(job.child.poll()) is not int
                        or job.status == "OUTCOME_UNKNOWN"
                        or job.tree is None or job.tree._handle is not None):
                    raise BrokerOwnerProcessError("child shutdown unconfirmed; lease retained")

    def close(self):
        if self._closed:
            return
        if self._thread_id is not None:
            self._same_thread()
        self._active = False
        self._closing = self._attempted = True
        if self._manager is not None:
            if self._manager_close_failed:
                raise BrokerOwnerProcessError("manager close failed; lease retained")
            if not self._manager_close_attempted:
                self._manager_close_attempted = True
                try:
                    # Runtime owns manager.close: invoke it exactly once. Partial
                    # runtime construction still requires direct manager cleanup.
                    if self._runtime is not None:
                        self._runtime.close()
                    else:
                        self._manager.close()
                except BaseException:
                    self._manager_close_failed = True
                    raise
            self._confirm_shutdown()
        if self._lease is not None:
            self._lease.close()
        self._runtime = self._manager = self._lease = self._guard = self._identity = None
        self._closed = True

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False
