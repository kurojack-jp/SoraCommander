"""Single-use local demo records; binding is NOT OS Owner authentication."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from time import monotonic
from threading import RLock
from uuid import uuid4

from .commands import Command


def binding(request: Command, context: tuple) -> str:
    data = json.dumps([asdict(request), context], sort_keys=True, ensure_ascii=True)
    return sha256(data.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Pending:
    pending_id: str
    request: Command
    digest: str
    expires_at: float


class LocalApproval:
    # Same-Host, bounded replay memory only. NOT durable Broker admission.
    _RECENT_SECONDS = 600
    _RECENT_CAPACITY = 1024

    def __init__(self, clock=monotonic):
        self.clock = clock
        self.lock = RLock()
        self._pending: dict[str, Pending] = {}
        self._recent: dict[str, tuple[Pending, float]] = {}

    def create(self, request: Command, context: tuple) -> Pending:
        with self.lock:
            now = self.clock()
            self._pending = {k: p for k, p in self._pending.items()
                             if now < p.expires_at}
            self._recent = {k: pair for k, pair in self._recent.items()
                            if now < pair[1] + self._RECENT_SECONDS}
            digest = binding(request, context)
            # Pending AND just-completed same-Host requests use the original
            # pending ID. A consumed review cannot open a second GUI.
            for existing in self._pending.values():
                if existing.request.request_id == request.request_id:
                    if existing.request != request or existing.digest != digest:
                        raise PermissionError("conflicting pending request ID")
                    return existing
            prior = self._recent.get(request.request_id)
            if prior is not None:
                record = prior[0]
                if record.request != request or record.digest != digest:
                    raise PermissionError("conflicting recent request ID")
                return record
            if len(self._pending) >= 128:
                raise RuntimeError("pending review capacity reached")
            record = Pending(uuid4().hex, request, digest, now + 60)
            self._pending[record.pending_id] = record
            if len(self._recent) >= self._RECENT_CAPACITY:
                # Bounded history; never a substitute for protected durable
                # replay prevention across pruning or process restarts.
                self._recent.pop(next(iter(self._recent)))
            self._recent[request.request_id] = (record, now)
            from .activity import publish
            publish(getattr(self, "_activity_store", None), request_id=request.request_id,
                    pending_id=record.pending_id, state="PENDING", review_state="PENDING", source="review")
            return record

    def activity_snapshot(self):
        """Pure logical-window identity snapshot, without pruning or consent."""
        with self.lock:
            now = self.clock()
            return tuple((p.request.request_id, p.pending_id,
                          p.pending_id in self._pending and now < p.expires_at)
                         for p, admitted in self._recent.values()
                         if now < admitted + self._RECENT_SECONDS)

    def pending_id_for_request_local(self, request_id: str):
        """Read-only lookup for recent same-Host Owner reviews, not consent.

        Includes consumed or denied records until the local recent window
        expires. Knowing a request ID cannot create or approve a review.
        """
        with self.lock:
            pair = self._recent.get(request_id)
            if pair is None or self.clock() >= pair[1] + self._RECENT_SECONDS:
                return None
            return pair[0].pending_id

    def matches(self, pending_id: str, request: Command, context: tuple) -> bool:
        with self.lock:
            record = self._pending.get(pending_id)
            return bool(record and self.clock() < record.expires_at and record.digest == binding(request, context))

    def consume_local(self, pending_id: str, context: tuple) -> Pending:
        with self.lock:
            record = self._pending.pop(pending_id, None)
            if not record or self.clock() >= record.expires_at or record.digest != binding(record.request, context):
                raise PermissionError("review unavailable or stale")
            return record

    def reject_local(self, pending_id: str):
        with self.lock:
            if self._pending.pop(pending_id, None) is None:
                raise KeyError("review unavailable")

    def pending_local(self):
        with self.lock:
            return tuple(p for p in self._pending.values() if self.clock() < p.expires_at)


def review_diagnostics(stage, reason, approval_state, launch_outcome):
    """Fixed vocabulary only; no request text or exception text is diagnostic."""
    choices = {
        'stage': {'OWNER_CHECK', 'CONSUME', 'MONITOR', 'SECOND_GUI', 'REVALIDATE', 'LAUNCH', 'FIRST_GUI'},
        'reason': {'AUTHORIZATION_FAILED', 'CONFIRMATION_UNAVAILABLE', 'DENIED', 'APPROVED',
                   'EXPIRED', 'CLOSED', 'LAUNCH_NOT_STARTED', 'LAUNCH_UNKNOWN', 'LAUNCH_ACCEPTED'},
        'approval_state': {'NOT_CONSUMED', 'CONSUMED', 'SECOND_APPROVED', 'REJECTED', 'EXPIRED', 'UNKNOWN'},
        'launch_outcome': {'NOT_STARTED', 'UNKNOWN', 'STARTED'},
    }
    values = dict(zip(choices, (stage, reason, approval_state, launch_outcome)))
    if any(type(value) is not str or value not in choices[key] for key, value in values.items()):
        raise ValueError('invalid review diagnostic')
    return values


def diagnostic_fields(result):
    """Allowlist transport projection; discard arbitrary exception payloads."""
    try:
        fields = review_diagnostics(*(result[k] for k in
            ('stage', 'reason', 'approval_state', 'launch_outcome')))
    except (KeyError, ValueError):
        fields = {}
    if (result.get('status') == 'TIMED_OUT'
            and result.get('auto_retry') is False
            and result.get('required_action') == 'REPORT_AND_WAIT'):
        fields.update(auto_retry=False, required_action='REPORT_AND_WAIT')
    if type(result.get('error')) is str and result['error'] in {'OwnerAuthorizationRequired', 'OwnerConfirmationUnavailable',
                              'LaunchNotStarted', 'LaunchOutcomeUnknown'}:
        fields['error'] = result['error']
    if type(result.get('launch_phase')) is str and result['launch_phase'] in {'pre_spawn', 'spawning', 'spawned_running',
            'spawned_suspended', 'resuming', 'resumed', 'supervised'}:
        fields['launch_phase'] = result['launch_phase']
    return fields
