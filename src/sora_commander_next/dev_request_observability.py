"""Isolated DEV-only observations. No runtime imports, I/O, or authority.

Missing events never prove non-execution. See the P1 scope document.
"""

from dataclasses import dataclass
from enum import Enum
import json
from threading import Lock
from uuid import uuid4


MAX_EVENTS = 256
MAX_BYTES = 64 * 1024


class EventKind(Enum):
    ADAPTER_ENTER = "ADAPTER_ENTER"
    VALIDATION_REJECT = "VALIDATION_REJECT"
    HANDLER_ENTER = "HANDLER_ENTER"
    HANDLER_EXIT = "HANDLER_EXIT"
    ADAPTER_EXIT = "ADAPTER_EXIT"


class Outcome(Enum):
    OK = "OK"
    DENIED = "DENIED"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class ReasonCode(Enum):
    NONE = "NONE"
    INVALID_REQUEST = "INVALID_REQUEST"
    ACCESS_DENIED = "ACCESS_DENIED"
    HANDLER_FAILURE = "HANDLER_FAILURE"
    REQUEST_CANCELLED = "REQUEST_CANCELLED"
    UNDETERMINED = "UNDETERMINED"


class RecorderState(Enum):
    OPEN = "OPEN"
    FULL = "FULL"
    INVALID_INPUT = "INVALID_INPUT"
    RECORD_ERROR = "RECORD_ERROR"


@dataclass(frozen=True, slots=True, eq=False)
class RequestHandle:
    """Local identity only; reconstructed or foreign handles are rejected."""

    correlation_id: str


@dataclass(frozen=True, slots=True)
class Event:
    epoch: str
    sequence: int
    correlation_id: str
    kind: EventKind
    outcome: Outcome
    reason: ReasonCode


@dataclass(frozen=True, slots=True)
class Snapshot:
    epoch: str
    state: RecorderState
    events: tuple[Event, ...]
    byte_count: int


def _new_id() -> str:
    value = uuid4().hex
    if (type(value) is not str or len(value) != 32
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError("identifier unavailable")
    return value


def _encode_event(event: Event) -> bytes:
    # Canonical byte accounting only; this is never written or transmitted.
    return json.dumps({
        "epoch": event.epoch,
        "sequence": event.sequence,
        "correlation_id": event.correlation_id,
        "kind": event.kind.value,
        "outcome": event.outcome.value,
        "reason": event.reason.value,
    }, separators=(",", ":"), ensure_ascii=True).encode("ascii")


class DevRequestRecorder:
    """Bounded, lock-serialized, append-only recorder with a terminal latch.

    Only exact enum types and identities issued by this instance are accepted.
    Calls entering the method body return False/None on failure and never retry,
    evict, or store input/errors. Python argument-binding errors raise TypeError
    before the body runs and leave the recorder unchanged.
    This class is not an authorization, execution, or durable audit mechanism.
    """

    def __init__(self):
        self._lock = Lock()
        try:
            self._epoch = _new_id()
        except Exception:
            self._epoch = None
        # Raise after leaving the handler so our entropy exception is not chained.
        # An exception being handled by the caller can still become __context__.
        if self._epoch is None:
            raise RuntimeError("recorder unavailable") from None
        self._state = RecorderState.OPEN
        self._requests: tuple[RequestHandle, ...] = ()
        self._events: tuple[Event, ...] = ()
        self._byte_count = 0

    def begin_request(self, **unexpected) -> RequestHandle | None:
        """Mint a local correlation handle; no client ID or request input."""
        with self._lock:
            if self._state is not RecorderState.OPEN:
                return None
            if unexpected:
                self._state = RecorderState.INVALID_INPUT
                return None
            if len(self._requests) >= MAX_EVENTS or len(self._events) >= MAX_EVENTS:
                self._state = RecorderState.FULL
                return None
            try:
                correlation_id = _new_id()
                if (correlation_id == self._epoch or any(
                        item.correlation_id == correlation_id for item in self._requests)):
                    self._state = RecorderState.RECORD_ERROR
                    return None
                handle = RequestHandle(correlation_id)
                self._requests = self._requests + (handle,)
                return handle
            except Exception:
                self._state = RecorderState.RECORD_ERROR
                return None

    def record(self, request, kind, outcome=Outcome.UNKNOWN,
               reason=ReasonCode.NONE, **unexpected) -> bool:
        """True only after commit; binding TypeError leaves state unchanged."""
        with self._lock:
            if self._state is not RecorderState.OPEN:
                return False
            # Exact types before equality/serialization: no untrusted hooks.
            if (unexpected or type(request) is not RequestHandle
                    or type(kind) is not EventKind or type(outcome) is not Outcome
                    or type(reason) is not ReasonCode
                    or not any(request is issued for issued in self._requests)):
                self._state = RecorderState.INVALID_INPUT
                return False
            if len(self._events) >= MAX_EVENTS:
                self._state = RecorderState.FULL
                return False
            try:
                event = Event(self._epoch, len(self._events) + 1,
                              request.correlation_id, kind, outcome, reason)
                encoded = _encode_event(event)
                if type(encoded) is not bytes or not encoded:
                    self._state = RecorderState.RECORD_ERROR
                    return False
                byte_count = self._byte_count + len(encoded)
                if byte_count > MAX_BYTES:
                    self._state = RecorderState.FULL
                    return False
                events = self._events + (event,)
                # All fallible construction precedes publication under the lock.
                self._events = events
                self._byte_count = byte_count
                return True
            except Exception:
                self._state = RecorderState.RECORD_ERROR
                return False

    def snapshot(self) -> Snapshot:
        """Immutable coherent copy of observations, never absence evidence."""
        with self._lock:
            return Snapshot(self._epoch, self._state, self._events, self._byte_count)
