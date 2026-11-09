"""U2-e DEV_ONLY single-actor lifecycle, never an execution authorization.

An entirely in-process simulator for reasoning about Owner approval,
CONSUME reply correlation and a single possible launch *record*. It has NO
OS/GUI/IPC/process/ledger service side effects. It cannot attest that any
service is authenticated: a caller may forge all input and all responses.

NO method on this class returns permission to spawn. Do not import into
production. Offline DA​​TA_ONLY states do not satisfy COMMIT-before-spawn.
"""
from dataclasses import dataclass
from enum import Enum
from threading import RLock

from .offline_b01_full_binding import (
    ContractError, FrozenCommand,
    decode_wire_request, make_wire_request,
)
from .offline_b01_receipt_adjudication import (
    OfflineReceiptAdjudicator, TestOnlyReplyContext,
)


class Phase(str, Enum):
    NEW = "NEW"
    FROZEN = "FROZEN"
    OWNER_APPROVED = "OWNER_APPROVED"
    CONSUMING = "CONSUMING"
    FRESH_OBSERVED_DATA_ONLY = "FRESH_OBSERVED_DATA_ONLY"
    SPAWN_ATTEMPT_RECORDED_DATA_ONLY = "SPAWN_ATTEMPT_RECORDED_DATA_ONLY"
    DENIED = "DENIED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ActorSnapshot:
    phase: str
    freeze_sha256: str | None
    wire_request_sha256: str | None
    consume_attempts: int
    spawn_attempt_records: int
    notes: tuple[str, ...]
    authority: str = "NONE_DATA_ONLY"


def _same_exact(a, b):
    return type(a) is type(b) and a == b


class OfflineBrokerActor:
    """Serialized transition model; single explicit request, no replay.

    Owner approval is only a data event and the 'fresh' reply is untrusted
    simulated wire bytes, NOT a verified service ACK. A real Broker would
    need an independent verified service channel + durable spend proof,
    active Owner token, release, profile and process/job checks.
    """

    def __init__(self, *, operation_id):
        if type(operation_id) is not str:
            raise ContractError("operation ID must be explicit")
        self._operation_id = operation_id
        self._lock = RLock()
        self._phase = Phase.NEW
        self._frozen: FrozenCommand | None = None
        self._pending_request: bytes | None = None
        self._receipt = None
        self._consume_attempts = 0
        self._spawn_attempt_records = 0
        self._notes: list[str] = []

    def inspect_data_only(self):
        from hashlib import sha256
        with self._lock:
            return ActorSnapshot(
                phase=self._phase.value,
                freeze_sha256=self._frozen.sha256 if self._frozen else None,
                wire_request_sha256=(
                    sha256(self._pending_request).hexdigest()
                    if self._pending_request else None),
                consume_attempts=self._consume_attempts,
                spawn_attempt_records=self._spawn_attempt_records,
                notes=tuple(self._notes),
            )

    def freeze_data_only(self, snapshot):
        with self._lock:
            if self._phase is not Phase.NEW:
                raise ContractError("freeze can occur only once")
            if type(snapshot) is not FrozenCommand:
                raise ContractError("verified canonical command bytes required")
            # FrozenCommand.__post_init__ already verifies canonical SHA;
            # revalidate so unsafe forged Python objects never bypass this.
            from .offline_b01_full_binding import parse_command
            detached = parse_command(snapshot.canonical_bytes)
            if detached != snapshot:
                raise ContractError("mismatched frozen command")
            # Do not retain the caller's dataclass: object.__setattr__ can
            # mutate even a frozen instance after the asserted Owner choice.
            self._frozen = detached
            self._phase = Phase.FROZEN
            self._notes.append("COMMAND_BYTES_FROZEN_DATA_ONLY")
            return self.inspect_data_only()

    def record_owner_choice_data_only(self, *, approved, presented_sha256):
        with self._lock:
            if self._phase is not Phase.FROZEN:
                raise ContractError("choice is available once only")
            if type(approved) is not bool or type(presented_sha256) is not str:
                raise ContractError("explicit choice and digest required")
            if not approved or presented_sha256 != self._frozen.sha256:
                self._phase = Phase.DENIED
                self._notes.append("CHOICE_DENIED_OR_PREVIEW_CHANGED")
            else:
                self._phase = Phase.OWNER_APPROVED
                self._notes.append("ASSERTED_OWNER_CHOICE_DATA_ONLY")
            return self.inspect_data_only()

    def begin_consume_data_only(self, *, tick_ms=0, connection_epoch=0):
        """Construct exactly one byte request; never call an actual service."""
        with self._lock:
            if self._phase is not Phase.OWNER_APPROVED:
                raise ContractError("consume not allowed from current state")
            # Generate/validate before irrevocably changing state.
            wire = make_wire_request(
                self._frozen, operation_id=self._operation_id,
                action="CONSUME")
            decode_wire_request(wire)
            try:
                receipt = OfflineReceiptAdjudicator(
                    command=self._frozen, expected_request=wire,
                    started_tick_ms=tick_ms, connection_epoch=connection_epoch)
            except ContractError:
                self._phase = Phase.UNKNOWN
                self._notes.append("INVALID_PENDING_RECEIPT_CONTEXT")
                raise
            self._receipt = receipt
            self._pending_request = wire
            self._consume_attempts += 1
            self._phase = Phase.CONSUMING
            self._notes.append("ONE_CONSUME_ATTEMPT_DATA_ONLY")
            return wire

    def observe_reply_data_only(
            self, raw, *, path, tick_ms=0, connection_epoch=0,
            generation=None, broker_instance_id=None, cancelled=False,
            restarted=False):
        """Observe matched simulated reply ONLY on original CONSUME path.

        'path' is a local test marker, never service authentication. QUERY,
        late ACK, stale action/generation/challenge and any unrecognized
        status cannot create another spawn-attempt record.
        """
        with self._lock:
            if type(path) is not str or path not in ("ORIGINAL_CONSUME", "QUERY", "RECONNECTED"):
                if self._phase is Phase.CONSUMING:
                    self._phase = Phase.UNKNOWN
                    self._notes.append("INVALID_REPLY_CONTEXT_IS_TERMINAL")
                raise ContractError("reply context missing")
            if self._phase is not Phase.CONSUMING:
                # Ignore unexpected/out-of-order reply without altering
                # UNKNOWN/DENIED/recorded irreversible terminal state.
                self._notes.append("IGNORED_LATE_OR_DUPLICATE_REPLY")
                return self.inspect_data_only()
            expected = decode_wire_request(self._pending_request)
            observation = self._receipt.adjudicate_data_only(
                raw, context=TestOnlyReplyContext(
                    path=path, connection_epoch=connection_epoch,
                    generation=(expected["generation"] if generation is None else generation),
                    broker_instance_id=(expected["broker_instance_id"]
                                        if broker_instance_id is None else broker_instance_id),
                    tick_ms=tick_ms, cancelled=cancelled, restarted=restarted))
            self._notes.append(observation.reason)
            if observation.decision == "DATA_UNTRUSTED_FRESH":
                self._phase = Phase.FRESH_OBSERVED_DATA_ONLY
                self._notes.append("UNTRUSTED_FRESH_BYTES_NOT_AUTHORITY")
            elif observation.decision == "DATA_DENIED":
                self._phase = Phase.DENIED
                self._notes.append("CONSUME_REJECTED")
            else:
                # SPENT, ABSENT (invalid for consume), UNKNOWN and any
                # degraded outcome never create a second opportunity.
                self._phase = Phase.UNKNOWN
                self._notes.append("NO_FRESH_CONSUME_EVIDENCE")
            return self.inspect_data_only()

    def observe_fixture_result_data_only(self, status):
        """Close the pending reply on U2-d data, never a synthetic fresh ACK."""
        with self._lock:
            if self._phase is not Phase.CONSUMING:
                raise ContractError("no pending fixture attempt")
            observation = self._receipt.observe_fixture_result_data_only(status)
            self._phase = Phase.UNKNOWN
            self._notes.append(observation.reason)
            return observation

    def record_spawn_attempt_data_only(self):
        """ONE synthetic record, not a capability, callable or real spawn."""
        with self._lock:
            if self._phase is not Phase.FRESH_OBSERVED_DATA_ONLY:
                raise ContractError("no unconsumed fresh observation")
            if self._spawn_attempt_records != 0:
                raise ContractError("duplicate spawn record")
            self._spawn_attempt_records = 1
            self._phase = Phase.SPAWN_ATTEMPT_RECORDED_DATA_ONLY
            self._notes.append("SIMULATED_SPAWN_RECORD_ONLY")
            return self.inspect_data_only()

    def mark_unknown_data_only(self, *, reason):
        """Any ambiguity becomes terminal; no automatic retry after query."""
        if type(reason) is not str or reason not in (
            "TIMEOUT", "DISCONNECT", "RESTART", "OWNER_LOCK",
            "SERVICE_UNVERIFIED", "COMMIT_AMBIGUOUS",
            "PROCESS_START_UNCERTAIN", "BROKER_SHUTDOWN"):
            raise ContractError("bounded uncertainty reason required")
        with self._lock:
            if self._phase in (Phase.DENIED, Phase.UNKNOWN,
                               Phase.SPAWN_ATTEMPT_RECORDED_DATA_ONLY):
                self._notes.append("UNKNOWN_EVENT_ON_TERMINAL_STATE")
                return self.inspect_data_only()
            self._phase = Phase.UNKNOWN
            self._notes.append("TERMINAL_UNKNOWN_" + reason)
            return self.inspect_data_only()

    def reopen_after_restart_data_only(self):
        """A new Broker may OBSERVE old records, never resurrect fresh."""
        with self._lock:
            # Even 'FRESH_OBSERVED' or previously 'APPROVED' cannot be
            # reconstructed into an executable grant across restart.
            if self._phase not in (Phase.DENIED, Phase.UNKNOWN,
                                   Phase.SPAWN_ATTEMPT_RECORDED_DATA_ONLY):
                self._phase = Phase.UNKNOWN
            self._notes.append("BROKER_RESTART_CANNOT_REARM")
            return self.inspect_data_only()
