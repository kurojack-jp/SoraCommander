"""Offline broker request gate; no transport, spawn, persistence, or authority."""
from dataclasses import dataclass
from threading import RLock
import hashlib
import hmac
import json
import math
import re
import time


class BrokerRequestDenied(ValueError):
    pass

# In-process consumed request IDs never expire or get evicted for capacity.
# Protected independent Broker persistence is still required across restart.
_MAX_CONSUMED_REQUESTS = 65536


def _hex(value, length):
    return type(value) is str and re.fullmatch("[0-9a-f]{" + str(length) + "}", value) is not None


def _bounded_text(value, maximum):
    return type(value) is str and 0 < len(value) <= maximum and "\0" not in value


@dataclass(frozen=True)
class BoundRequest:
    request_id: str
    executable: str
    argv: tuple[str, ...]
    cwd: str
    task_sha256: str
    owner_binding: str
    expires_at: float
    timeout: float
    output_limit: int

    def __post_init__(self):
        if not _hex(self.request_id, 32):
            raise BrokerRequestDenied("invalid request ID")
        if not _hex(self.task_sha256, 64):
            raise BrokerRequestDenied("invalid task digest")
        if not _bounded_text(self.owner_binding, 256):
            raise BrokerRequestDenied("invalid owner binding")
        if not _bounded_text(self.executable, 8192) or not _bounded_text(self.cwd, 8192):
            raise BrokerRequestDenied("invalid fixed paths")
        if type(self.argv) is not tuple or len(self.argv) > 32 or any(
            type(x) is not str or "\0" in x or len(x) > 4096 for x in self.argv
        ):
            raise BrokerRequestDenied("invalid argv")
        if type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at):
            raise BrokerRequestDenied("invalid expiry")
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 0.01 <= self.timeout <= 3600:
            raise BrokerRequestDenied("invalid timeout")
        if type(self.output_limit) is not int or not 128 <= self.output_limit <= 65536:
            raise BrokerRequestDenied("invalid output limit")

    def fingerprint(self):
        fields = (self.request_id, self.executable, self.argv, self.cwd,
                  self.task_sha256, self.owner_binding, self.expires_at,
                  self.timeout, self.output_limit)
        return hashlib.sha256(json.dumps(fields, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()


class BrokerAdmission:
    """One-use in-memory gate with separate active slots and spent IDs.

    Only a trusted local BrokerRuntime can release an active slot after
    verifying that its exact JobManager child exited; releasing a slot NEVER
    refunds an approved request ID. No IPC/client-side authority is implied.
    """

    def __init__(self, expected_executable, expected_cwd, owner_binding, clock=time.time, capacity=4):
        if type(capacity) is not int or not 1 <= capacity <= 16:
            raise ValueError("invalid capacity")
        if not _bounded_text(expected_executable, 8192) or not _bounded_text(expected_cwd, 8192):
            raise ValueError("invalid fixed scope")
        if not _bounded_text(owner_binding, 256) or not callable(clock):
            raise ValueError("invalid owner/clock")
        self.expected_executable = expected_executable
        self.expected_cwd = expected_cwd
        self.owner_binding = owner_binding
        self.clock = clock
        self.capacity = capacity
        self._accepted = {}  # all consumed IDs, for this process lifetime
        self._active = set()  # admitted jobs not yet independently verified done
        self._lock = RLock()

    def active_ids_local(self):
        """Observation only; never an execution grant or Client API."""
        with self._lock:
            return tuple(self._active)

    def note_verified_terminal_local(self, request_id):
        """Trusted Broker-only transition. No consent replay, even after finish.

        Caller must have separately verified a retained owned JobManager
        session's terminal outcome and actual exited OS child.
        """
        with self._lock:
            if request_id not in self._accepted:
                raise BrokerRequestDenied("unknown admission")
            if request_id not in self._active:
                return False
            self._active.remove(request_id)
            return True

    def admit(self, request, *, approved_fingerprint, current_task_sha256, owner_verified):
        if not isinstance(request, BoundRequest) or owner_verified is not True:
            raise BrokerRequestDenied("Owner approval required")
        if (request.executable != self.expected_executable
                or request.cwd != self.expected_cwd
                or request.owner_binding != self.owner_binding):
            raise BrokerRequestDenied("request outside fixed scope")
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now >= request.expires_at:
            raise BrokerRequestDenied("approval expired or clock unavailable")
        if not _hex(current_task_sha256, 64) or not hmac.compare_digest(request.task_sha256, current_task_sha256):
            raise BrokerRequestDenied("task content changed")
        digest = request.fingerprint()
        if not _hex(approved_fingerprint, 64) or not hmac.compare_digest(digest, approved_fingerprint):
            raise BrokerRequestDenied("approval mismatch")
        with self._lock:
            if request.request_id in self._accepted:
                raise BrokerRequestDenied("request already consumed")
            if len(self._accepted) >= _MAX_CONSUMED_REQUESTS:
                raise BrokerRequestDenied("broker replay guard full")
            if len(self._active) >= self.capacity:
                raise BrokerRequestDenied("broker capacity reached")
            self._accepted[request.request_id] = digest
            self._active.add(request.request_id)
        return {"status": "ACCEPTED", "request_id": request.request_id}
