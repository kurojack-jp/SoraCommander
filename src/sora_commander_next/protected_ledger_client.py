"""DEV_ONLY service-client candidate. No protected channel or spawn authority.

Reuses U2-b canonical wire/full command and U2-g one-shot reply adjudication.
No SQLite fixture, service control, pipe connect, callback spawner or native
transport is implemented. Product composition does not import this module.
"""
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic_ns

from .broker_frames import pack_frame, read_frame
from .broker_host import BrokerUnavailable
from .offline_b01_full_binding import (
    ContractError, FrozenCommand, MAX_SNAPSHOT, _decode, parse_command,
    make_wire_request, decode_wire_request, decode_wire_reply,
)
from .offline_b01_receipt_adjudication import OfflineReceiptAdjudicator, TestOnlyReplyContext
from .service_native_prerequisites import MISSING_EVIDENCE


@dataclass(frozen=True, slots=True)
class LedgerCallBinding:
    owner_sid: str
    logon_luid: tuple
    wts_session: int
    generation: str
    broker_instance_id: str
    execution_profile_sha256: str
    connection_epoch: int


@dataclass(frozen=True, slots=True)
class LedgerExchange:
    observation: str
    reason: str
    source_status: str | None = None
    authority: str = field(default="NONE_DATA_ONLY", init=False)
    admission: str = field(default="UNAVAILABLE", init=False)
    missing_evidence: tuple = field(default=MISSING_EVIDENCE, init=False)


class _UnavailableTransport:
    def exchange(self, frame):
        raise BrokerUnavailable("release-authenticated service transport unavailable")


def _unpack(raw):
    if type(raw) is not bytes or not 4 < len(raw) <= 8196:
        raise ContractError("bounded exact framed reply required")
    offset = 0

    def take(size):
        nonlocal offset
        part = raw[offset:offset + size]
        offset += size
        return part

    payload = read_frame(take)
    if offset != len(raw):
        raise ContractError("trailing frame bytes")
    return payload


class ProtectedLedgerClient:
    """One bounded volatile history, no rearm/reconnect/reset/positive permit.

    _transport and _clock_ms are private unit-test seams. Injected native-shaped
    transport is still untrusted; even exact COMMITTED_FRESH yields data only.
    Reconstructing this object is NOT durable replay protection. The real
    protected writer and independently accepted channel remain absent.
    """
    def __init__(self, *, _transport=None, _clock_ms=None):
        self._transport = _transport if _transport is not None else _UnavailableTransport()
        self._clock = _clock_ms if _clock_ms is not None else lambda: monotonic_ns() // 1_000_000
        self._slot = Lock()
        self._history = {}
        self._operations, self._challenges = set(), set()
        self._last_tick = -1
        self._halted = False

    def _tick(self):
        now = self._clock()
        if type(now) is not int or not 0 <= now < 1 << 63 or now < self._last_tick:
            self._halted = True
            raise ContractError("invalid or rolled back monotonic clock")
        self._last_tick = now
        return now

    @staticmethod
    def _binding(command, binding):
        if type(command) is not FrozenCommand or parse_command(command.canonical_bytes) != command:
            raise ContractError("exact revalidated command required")
        data = _decode(command.canonical_bytes, MAX_SNAPSHOT)
        if (type(binding) is not LedgerCallBinding
                or type(binding.logon_luid) is not tuple
                or any(type(n) is not int for n in binding.logon_luid)
                or type(binding.wts_session) is not int
                or type(binding.connection_epoch) is not int
                or not 1 <= binding.connection_epoch < 1 << 63):
            raise ContractError("exact local call binding required")
        for name in ("owner_sid", "wts_session", "generation", "broker_instance_id",
                     "execution_profile_sha256"):
            if type(getattr(binding, name)) is not type(data[name]) or getattr(binding, name) != data[name]:
                raise ContractError("local call context mismatch")
        if list(binding.logon_luid) != data["logon_luid"]:
            raise ContractError("local logon context mismatch")
        return data

    def consume_once(self, command, *, operation_id, binding, current_binding):
        """Send at most one CONSUME; current_binding is a local test observer.

        It does not authenticate Owner/session. There is intentionally no
        approved/trusted flag and no launch callback in this API.
        """
        data = self._binding(command, binding)
        raw = make_wire_request(command, operation_id=operation_id)
        key = data["installation_id"], data["generation"], data["request_id"]
        if not self._slot.acquire(blocking=False):
            return LedgerExchange("UNAVAILABLE", "CLIENT_BUSY")
        try:
            if key in self._history:
                return LedgerExchange("DATA_IGNORED", "ATTEMPT_ALREADY_RETAINED")
            if self._halted or len(self._history) >= 64:
                return LedgerExchange("UNAVAILABLE", "ADMISSION_HALTED_OR_FULL")
            if operation_id in self._operations or data["approval_nonce"] in self._challenges:
                return LedgerExchange("DATA_DENIED", "OPERATION_OR_CHALLENGE_REUSE")
            # Reserve history BEFORE any callback/send. Exceptions cannot rearm.
            self._history[key] = LedgerExchange("OUTCOME_UNKNOWN", "EXCHANGE_UNCONFIRMED")
            self._operations.add(operation_id)
            self._challenges.add(data["approval_nonce"])
            result = self._history[key]
            try:
                started = self._tick()
                deadline = min(data["expires_at_tick_ms"], started + 5000)
                initial_binding = current_binding()
                self._binding(command, initial_binding)
                if started >= deadline or initial_binding != binding:
                    raise ContractError("stale initial context")
                if self._tick() >= deadline:
                    raise ContractError("context check exceeded call deadline")
                adjudicator = OfflineReceiptAdjudicator(command=command,
                    expected_request=raw, started_tick_ms=started,
                    connection_epoch=binding.connection_epoch)
                # Send only through injected test transport; the default refuses.
                try:
                    reply = self._transport.exchange(pack_frame(raw))
                except BrokerUnavailable:
                    # Only the transport can assert its documented NOT_SENT
                    # outcome. An observer raising the same exception after
                    # send cannot prove that COMMIT did not happen.
                    result = LedgerExchange("UNAVAILABLE", "PROTECTED_TRANSPORT_NOT_ACCEPTED")
                    self._history[key] = result
                    return result
                rebound = current_binding()
                self._binding(command, rebound)
                now = self._tick()
                if rebound != binding or now >= deadline:
                    raise ContractError("late reply or rebound context")
                observed = adjudicator.adjudicate_data_only(_unpack(reply), context=TestOnlyReplyContext(
                    "ORIGINAL_CONSUME", binding.connection_epoch, binding.generation,
                    binding.broker_instance_id, now))
                if self._tick() >= deadline:
                    raise ContractError("adjudication exceeded call deadline")
                result = LedgerExchange(observed.decision, observed.reason, observed.source_status)
            except Exception:
                pass  # Unknown remains terminal, no send retry or query rearm.
            self._history[key] = result
            return result
        finally:
            self._slot.release()

    def observe_query_reply(self, raw, *, expected_request):
        """Pure QUERY decoding; never updates consume history or yields fresh."""
        expected = decode_wire_request(expected_request)
        if expected["action"] != "QUERY":
            raise ContractError("QUERY required")
        value = decode_wire_reply(_unpack(raw), expected_request=expected)
        return LedgerExchange("DATA_QUERY_ONLY", "QUERY_CANNOT_REARM", value["status"])
