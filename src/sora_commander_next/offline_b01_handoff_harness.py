"""U2-f: DEV_ONLY end-to-end contract over U2-b/c/d/e mock components.

This deliberately cannot attest Windows identity, the service, Owner approval
or any native executable. It never promotes fixture DATA_* observations into
COMMITTED_FRESH or a process spawn capability. No IPC, GUI, service or process.
"""
from dataclasses import dataclass
from threading import RLock

from .offline_b01_broker_actor import OfflineBrokerActor
from .offline_b01_execution_profile import (
    FrozenExecutionProfile, prepare_command_profile_binding,
)
from .offline_b01_full_binding import (
    ContractError, FrozenCommand, _decode, MAX_SNAPSHOT,
)


_MAX_TICK = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class ClaimedOwnerContext:
    """Untrusted caller assertion, NOT an OS token or authentication result."""
    installation_id: str
    generation: str
    broker_instance_id: str
    owner_sid: str
    logon_luid: tuple[int, int]
    wts_session: int
    policy_generation: int
    release_sequence: int


@dataclass(frozen=True, slots=True)
class HandoffObservation:
    actor_phase: str
    ledger_result: str
    consumed_requests: int
    synthetic_spawn_records: int
    authority: str = "NONE_DATA_ONLY"


class OfflineHandoffHarness:
    """Exactly one synthetic Broker→U2-d ledger consume, bound to U2-c.

    All clocks and Windows identity values are *claims* supplied by tests.
    Even a "DATA_RECORDED_ONCE" outcome is terminal UNKNOWN in the actor,
    because an unprotected development fixture cannot authenticate a COMMIT.
    """

    def __init__(self, *, command_data, profile, operation_id):
        if type(profile) is not FrozenExecutionProfile:
            raise ContractError("execution profile is required")
        self._command: FrozenCommand = prepare_command_profile_binding(
            command_data, profile)
        self._fields = _decode(self._command.canonical_bytes, MAX_SNAPSHOT)
        self._actor = OfflineBrokerActor(operation_id=operation_id)
        self._actor.freeze_data_only(self._command)
        self._last_tick = None
        self._outcome = "NOT_REQUESTED"
        self._gate = RLock()

    @property
    def command_digest_data_only(self):
        return self._command.sha256

    def inspect_data_only(self):
        with self._gate:
            observed = self._actor.inspect_data_only()
            return HandoffObservation(
                actor_phase=observed.phase,
                ledger_result=self._outcome,
                consumed_requests=observed.consume_attempts,
                synthetic_spawn_records=observed.spawn_attempt_records,
            )

    def _check_asserted_preflight(self, *, claims, tick_ms):
        if type(tick_ms) is not int or not (0 <= tick_ms <= _MAX_TICK):
            raise ContractError("invalid claimed monotonic clock")
        if self._last_tick is not None and tick_ms < self._last_tick:
            raise ContractError("claimed clock moved backwards")
        # Recording last tick before checking deadline is deliberate:
        # a second call cannot rewind the time horizon.
        self._last_tick = tick_ms
        if type(claims) is not ClaimedOwnerContext:
            raise ContractError("exact claimed context type required")
        expected = self._fields
        pairs = (
            (claims.installation_id, expected["installation_id"]),
            (claims.generation, expected["generation"]),
            (claims.broker_instance_id, expected["broker_instance_id"]),
            (claims.owner_sid, expected["owner_sid"]),
            (claims.logon_luid, tuple(expected["logon_luid"])),
            (claims.wts_session, expected["wts_session"]),
            (claims.policy_generation, expected["policy_generation"]),
            (claims.release_sequence, expected["release_sequence"]),
        )
        if any(type(got) is not type(want) or got != want
               for got, want in pairs):
            raise ContractError("untrusted identity or profile claim mismatch")
        if tick_ms >= expected["expires_at_tick_ms"]:
            raise ContractError("claimed approval deadline expired")

    def record_asserted_owner_choice_data_only(
            self, *, approved, presented_digest, claims, tick_ms):
        with self._gate:
            try:
                self._check_asserted_preflight(claims=claims, tick_ms=tick_ms)
            except ContractError:
                self._actor.mark_unknown_data_only(reason="COMMIT_AMBIGUOUS")
                self._outcome = "PREFLIGHT_REJECTED"
                raise
            return self._actor.record_owner_choice_data_only(
                approved=approved, presented_sha256=presented_digest)

    def consume_fixture_data_only(self, *, fixture, claims, tick_ms):
        """Use a temporary DB once and unconditionally stop before any launch.

        Caller is allowed to QUERY the U2-d data-only fixture afterward,
        but QUERY cannot reactivate this actor or confer a fresh receipt.
        """
        with self._gate:
            try:
                self._check_asserted_preflight(claims=claims, tick_ms=tick_ms)
            except ContractError:
                self._actor.mark_unknown_data_only(reason="COMMIT_AMBIGUOUS")
                self._outcome = "PREFLIGHT_REJECTED"
                raise
            from .offline_b01_service_fixture import OfflineFixtureLedger
            if type(fixture) is not OfflineFixtureLedger:
                self._actor.mark_unknown_data_only(reason="SERVICE_UNVERIFIED")
                self._outcome = "NO_APPROVED_SERVICE"
                raise ContractError("U2-d temporary fixture only")
            wire = self._actor.begin_consume_data_only(tick_ms=tick_ms)
            try:
                ledger_result = fixture.apply_data_only(wire)
            except BaseException:
                # Even unexpected fixture failures cannot re-arm the Actor.
                self._actor.mark_unknown_data_only(reason="COMMIT_AMBIGUOUS")
                self._outcome = "DATA_UNKNOWN"
                raise
            observation = self._actor.observe_fixture_result_data_only(ledger_result)
            self._outcome = observation.source_status or "DATA_UNKNOWN"
            return self.inspect_data_only()

    def mark_disconnected_data_only(self):
        with self._gate:
            self._actor.mark_unknown_data_only(reason="DISCONNECT")
            return self.inspect_data_only()

    def observe_untrusted_reply_data_only(self, raw):
        """An unsolicited fresh reply cannot revive a terminal actor."""
        with self._gate:
            self._actor.observe_reply_data_only(
                raw, path="ORIGINAL_CONSUME")
            return self.inspect_data_only()

    def record_synthetic_spawn_data_only(self):
        """Always deny when driven by fixture, including DATA_RECORDED_ONCE."""
        with self._gate:
            return self._actor.record_spawn_attempt_data_only()
