"""Inert, per-process Owner-review queue for isolated design testing.

This module owns no Windows Owner verification, consent, grants, job execution,
IPC endpoint or permissions. NEVER interpret REVIEWED_DATA_ONLY as authority.
A future accepted *single protected Broker* may reuse the scheduling concept,
but needs independently validated Owner identity and durable admission.
"""
from collections import deque
from dataclasses import dataclass
from threading import RLock
from uuid import uuid4
import re

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RESULTS = frozenset((
    "DENIED_DATA_ONLY", "REVIEWED_DATA_ONLY", "UNKNOWN_DATA_ONLY",
))


@dataclass(frozen=True, slots=True)
class QueueDataOnly:
    status: str
    request_id: str | None = None
    queue_id: str | None = None
    fingerprint: str | None = None


class OfflineReviewQueue:
    """One FIFO reviewer across callers sharing a single Python instance.

    No client is allowed to choose a ticket. Neither a ticket nor a terminal
    result proves an Owner click or permits a Windows child to be spawned.
    """

    def __init__(self, *, pending_capacity=64, history_capacity=1024):
        if (type(pending_capacity) is not int
                or not 1 <= pending_capacity <= 128
                or type(history_capacity) is not int
                or not pending_capacity <= history_capacity <= 4096):
            raise ValueError("bounded queue capacities required")
        self._lock = RLock()
        self._pending_capacity = pending_capacity
        self._history_capacity = history_capacity
        self._entries = {}
        self._order = deque()
        self._active = None
        self._closed = False

    @staticmethod
    def _valid(request_id, fingerprint):
        return (type(request_id) is str and 1 <= len(request_id) <= 128
                and "\0" not in request_id
                and type(fingerprint) is str
                and _DIGEST.fullmatch(fingerprint) is not None)

    def submit_data_only(self, request_id, fingerprint):
        """Return existing same-scope review, else enqueue once; never approve."""
        if not self._valid(request_id, fingerprint):
            return QueueDataOnly("DENY_INVALID")
        with self._lock:
            if self._closed:
                return QueueDataOnly("DENY_CLOSED", request_id)
            previous = self._entries.get(request_id)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    return QueueDataOnly("DENY_CONFLICT", request_id)
                return previous
            waiting = len(self._order) + (self._active is not None)
            if (waiting >= self._pending_capacity
                    or len(self._entries) >= self._history_capacity):
                # Never evict an old ID to silently allow a duplicate review.
                return QueueDataOnly("BUSY", request_id)
            item = QueueDataOnly("QUEUED_DATA_ONLY", request_id,
                                 uuid4().hex, fingerprint)
            self._entries[request_id] = item
            self._order.append(request_id)
            return item

    def take_next_data_only(self):
        """Only the owning Broker loop should call this, never an MCP tool.

        This API by itself cannot authenticate a caller or the Owner and MUST
        NOT be exported over a remote/public transport.
        """
        with self._lock:
            if self._closed or self._active is not None or not self._order:
                return None
            request_id = self._order.popleft()
            existing = self._entries[request_id]
            current = QueueDataOnly("IN_REVIEW_DATA_ONLY",
                                    existing.request_id,
                                    existing.queue_id, existing.fingerprint)
            self._active = request_id
            self._entries[request_id] = current
            return current

    def complete_data_only(self, queue_id, result):
        """Complete only the active review; no notion of approval authority."""
        if (type(queue_id) is not str or type(result) is not str
                or result not in _RESULTS):
            return QueueDataOnly("DENY_INVALID")
        with self._lock:
            if self._closed or self._active is None:
                return QueueDataOnly("DENY_NO_ACTIVE")
            current = self._entries[self._active]
            if queue_id != current.queue_id:
                return QueueDataOnly("DENY_WRONG_TICKET")
            done = QueueDataOnly(result, current.request_id,
                                 current.queue_id, current.fingerprint)
            self._entries[current.request_id] = done
            self._active = None
            return done

    def observe_data_only(self, request_id, fingerprint):
        """Read-only status lookup; no dispatch and no new Owner dialog."""
        if not self._valid(request_id, fingerprint):
            return QueueDataOnly("DENY_INVALID")
        with self._lock:
            item = self._entries.get(request_id)
            if item is None:
                return QueueDataOnly("UNKNOWN", request_id)
            if item.fingerprint != fingerprint:
                return QueueDataOnly("DENY_CONFLICT", request_id)
            return item

    def close_data_only(self):
        """Mark unfinished reviews UNKNOWN; never start or replay any work."""
        with self._lock:
            if self._closed:
                return
            for request_id, item in tuple(self._entries.items()):
                if item.status in ("QUEUED_DATA_ONLY", "IN_REVIEW_DATA_ONLY"):
                    self._entries[request_id] = QueueDataOnly(
                        "UNKNOWN_DATA_ONLY", item.request_id,
                        item.queue_id, item.fingerprint)
            self._active = None
            self._order.clear()
            self._closed = True
