"""U2-g DEV_ONLY receipt correlation. All bytes/context are test assertions.

This boundary verifies data shape and one pending attempt, NOT a service or
OS identity. Even DATA_UNTRUSTED_FRESH has no permission value. No callable,
handle, capability, launch admission or production integration is provided.
Reconstructing a new object is NOT durable replay prevention across restarts.
"""
from dataclasses import dataclass, field
from threading import RLock

from .offline_b01_full_binding import (
    ContractError, FrozenCommand, MAX_SNAPSHOT, _decode, decode_wire_reply,
    decode_wire_request, make_wire_request, parse_command,
)

_MAX_TICK = (1 << 63) - 1
_FIXTURE_STATUSES = frozenset((
    "DATA_RECORDED_ONCE", "DATA_SPENT", "DATA_UNKNOWN", "DATA_DENIED",
    "DATA_CONFLICT", "DATA_ABSENT", "DATA_FULL",
))


def _tick(value):
    if type(value) is not int or not 0 <= value <= _MAX_TICK:
        raise ContractError("bounded exact integer required")
    return value


@dataclass(frozen=True, slots=True)
class TestOnlyReplyContext:
    """Untrusted local markers; never native channel/token verification."""
    path: str
    connection_epoch: int
    generation: str
    broker_instance_id: str
    tick_ms: int
    cancelled: bool = False
    restarted: bool = False


@dataclass(frozen=True, slots=True)
class ReceiptObservation:
    decision: str
    reason: str
    source_status: str | None = None
    authority: str = field(default="NONE_DATA_ONLY", init=False)


class OfflineReceiptAdjudicator:
    """Consume one reply opportunity, including invalid replies, under a lock.

    The frozen command binds profile digest, challenge, request and generation;
    expected_request must be its exact derived CONSUME frame. No setter/reset,
    reconnect, QUERY or deserialize/rearm interface exists. Connection epochs,
    clocks and identity fields remain caller assertions, not actual OS proof.
    """

    def __init__(self, *, command, expected_request, started_tick_ms,
                 connection_epoch):
        if type(command) is not FrozenCommand:
            raise ContractError("exact frozen command required")
        frozen = parse_command(command.canonical_bytes)
        if frozen != command:
            raise ContractError("forged frozen command")
        expected = decode_wire_request(expected_request)
        if (expected["action"] != "CONSUME" or expected_request !=
                make_wire_request(frozen, operation_id=expected["operation_id"])):
            raise ContractError("pending CONSUME must match frozen command")
        fields = _decode(frozen.canonical_bytes, MAX_SNAPSHOT)
        self._deadline = fields["expires_at_tick_ms"]
        self._started = _tick(started_tick_ms)
        self._epoch = _tick(connection_epoch)
        if self._started >= self._deadline:
            raise ContractError("pending CONSUME already expired")
        self._expected = expected
        self._pending = True
        self._lock = RLock()

    def _context_reason(self, context):
        if type(context) is not TestOnlyReplyContext:
            return "INVALID_TEST_CONTEXT"
        if (type(context.cancelled) is not bool or
                type(context.restarted) is not bool):
            return "INVALID_TEST_CONTEXT"
        try:
            tick = _tick(context.tick_ms)
            epoch = _tick(context.connection_epoch)
        except ContractError:
            return "INVALID_TEST_CONTEXT"
        if context.cancelled or context.restarted:
            return "CANCELLED_OR_RESTARTED"
        if tick < self._started or tick >= self._deadline:
            return "STALE_OR_EXPIRED"
        if (type(context.path) is not str or
                context.path != "ORIGINAL_CONSUME" or epoch != self._epoch):
            return "NOT_ORIGINAL_CONNECTION"
        if any(type(got) is not str or got != want for got, want in (
                (context.generation, self._expected["generation"]),
                (context.broker_instance_id, self._expected["broker_instance_id"]))):
            return "STALE_GENERATION_OR_BROKER"
        return None

    def adjudicate_data_only(self, raw, *, context):
        with self._lock:
            if not self._pending:
                return ReceiptObservation("DATA_IGNORED", "ATTEMPT_ALREADY_OBSERVED")
            # Invalid schema/context also consumes the opportunity: never retry
            # a corrected response for the same attempt after ambiguity.
            self._pending = False
            reason = self._context_reason(context)
            if reason:
                return ReceiptObservation("DATA_UNKNOWN", reason)
            try:
                reply = decode_wire_reply(raw, expected_request=self._expected)
            except (ContractError, ValueError, TypeError):
                return ReceiptObservation("DATA_UNKNOWN", "INVALID_OR_MISMATCHED_REPLY")
            status = reply["status"]
            if status == "COMMITTED_FRESH":
                return ReceiptObservation(
                    "DATA_UNTRUSTED_FRESH", "SYNTHETIC_BYTES_NOT_OS_PROOF", status)
            if status in ("DENIED", "CONFLICT"):
                return ReceiptObservation("DATA_DENIED", "CONSUME_REJECTED", status)
            return ReceiptObservation("DATA_UNKNOWN", "NO_FRESH_OBSERVATION", status)

    def observe_fixture_result_data_only(self, status):
        """Separate U2-d dialect; never normalize DATA_* into wire COMMIT."""
        with self._lock:
            if not self._pending:
                return ReceiptObservation("DATA_IGNORED", "ATTEMPT_ALREADY_OBSERVED")
            self._pending = False
            if type(status) is not str or status not in _FIXTURE_STATUSES:
                return ReceiptObservation("DATA_UNKNOWN", "INVALID_FIXTURE_STATUS")
            return ReceiptObservation("DATA_UNKNOWN", "FIXTURE_NOT_SERVICE_PROOF", status)
