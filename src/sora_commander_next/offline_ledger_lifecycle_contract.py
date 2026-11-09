"""DEV_ONLY lifecycle data model; no OS calls, storage, IPC or authority.

Caller supplies monotonic timestamps. Reserving an observation does NOT perform
or authorize a service operation. Copies/new instances do not enforce global
limits or persistent replay protection. No runtime imports this model.
"""
from dataclasses import dataclass, replace
from enum import Enum
import math
import re


class RegistrationPhase(Enum):
    ACTIVE = "ACTIVE"
    QUIESCING = "QUIESCING"
    REVOKED = "REVOKED"
    PREPARED = "PREPARED"


class RecoveryEvent(Enum):
    BEGIN = "BEGIN"
    QUIESCENCE_OBSERVED = "QUIESCENCE_OBSERVED"
    REPLACEMENT_PREPARED = "REPLACEMENT_PREPARED"
    ACTIVATE = "ACTIVATE"


def _generation(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise ValueError("exact generation identifier required")
    return value


@dataclass(frozen=True, slots=True)
class RegistrationState:
    """Offline recovery record model, NOT a registration or authorization API.

    Events are supplied observations, never proof of OS quiescence, durable
    writes or administrator approval. No generation is generated or persisted.
    An untrusted/stale ACTIVE record is NOT detected by this data model.
    """

    generation: str
    phase: RegistrationPhase = RegistrationPhase.ACTIVE
    replacement_generation: str | None = None
    retired_generations: tuple[str, ...] = ()

    def __post_init__(self):
        _generation(self.generation)
        if type(self.retired_generations) is not tuple or len(self.retired_generations) > 1024:
            raise ValueError("bounded immutable retirement history required")
        for generation in self.retired_generations:
            _generation(generation)
        if (len(set(self.retired_generations)) != len(self.retired_generations)
                or self.generation in self.retired_generations):
            raise ValueError("inconsistent retirement history")
        if type(self.phase) is not RegistrationPhase:
            raise ValueError("typed registration phase required")
        if self.phase is RegistrationPhase.ACTIVE:
            if self.replacement_generation is not None:
                raise ValueError("active record cannot contain a replacement")
        else:
            _generation(self.replacement_generation)
            if (self.replacement_generation == self.generation
                    or self.replacement_generation in self.retired_generations
                    or len(self.retired_generations) >= 1024):
                raise ValueError("recovery must change generation")

    def models_current_generation(self, generation):
        """Compare data only; callers still need authenticated protected state."""
        _generation(generation)
        return self.phase is RegistrationPhase.ACTIVE and generation == self.generation

    def advance(self, event, *, replacement_generation=None):
        if type(event) is not RecoveryEvent:
            raise ValueError("typed recovery event required")
        if event is RecoveryEvent.BEGIN:
            if self.phase is not RegistrationPhase.ACTIVE:
                raise ValueError("recovery already in progress")
            return replace(self, phase=RegistrationPhase.QUIESCING,
                           replacement_generation=replacement_generation)
        if replacement_generation is not None:
            raise ValueError("cannot replace a pending recovery target")
        transitions = {
            (RegistrationPhase.QUIESCING, RecoveryEvent.QUIESCENCE_OBSERVED):
                RegistrationPhase.REVOKED,
            (RegistrationPhase.REVOKED, RecoveryEvent.REPLACEMENT_PREPARED):
                RegistrationPhase.PREPARED,
        }
        phase = transitions.get((self.phase, event))
        if phase is not None:
            return replace(self, phase=phase)
        if self.phase is RegistrationPhase.PREPARED and event is RecoveryEvent.ACTIVATE:
            return RegistrationState(
                self.replacement_generation,
                retired_generations=(*self.retired_generations, self.generation))
        raise ValueError("invalid recovery transition")

    def to_record(self):
        return {"version": 1, "generation": self.generation,
                "phase": self.phase.value,
                "replacement_generation": self.replacement_generation,
                "retired_generations": list(self.retired_generations)}

    @classmethod
    def from_record(cls, record):
        """Validate detached record data; no filesystem or trust verification."""
        if (type(record) is not dict
                or set(record) != {"version", "generation", "phase", "replacement_generation",
                                   "retired_generations"}
                or type(record["version"]) is not int or record["version"] != 1
                or type(record["phase"]) is not str
                or type(record["retired_generations"]) is not list
                or len(record["retired_generations"]) > 1024):
            raise ValueError("invalid registration record schema")
        return cls(record["generation"], RegistrationPhase(record["phase"]),
                   record["replacement_generation"], tuple(record["retired_generations"]))


class RegistrationComparison(Enum):
    MISSING_RECORD = "MISSING_RECORD"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    RECOVERY_IN_PROGRESS = "RECOVERY_IN_PROGRESS"
    HISTORY_MISMATCH = "HISTORY_MISMATCH"
    MATCHING_ACTIVE_DATA = "MATCHING_ACTIVE_DATA"


def compare_registration_data(*, expected_installation, anchor_installation,
                              database_installation, anchor, database):
    """Offline comparison only, never a health/admission/commit decision.

    Inputs must be detached observations. Matching stale or fabricated records
    cannot be detected. This function supplies no atomic read, OS authenticity,
    durable state, approval, or protection against a concurrent recovery.
    None represents a missing/unreadable observation, never first installation.
    """
    _generation(expected_installation)
    for installation, state in ((anchor_installation, anchor),
                                (database_installation, database)):
        if installation is not None:
            _generation(installation)
        if state is not None and type(state) is not RegistrationState:
            raise ValueError("typed registration observation required")
    if (anchor_installation is None or database_installation is None
            or anchor is None or database is None):
        return RegistrationComparison.MISSING_RECORD
    if (anchor_installation != expected_installation
            or database_installation != expected_installation
            or anchor.generation != database.generation):
        return RegistrationComparison.IDENTITY_MISMATCH
    if (anchor.phase is not RegistrationPhase.ACTIVE
            or database.phase is not RegistrationPhase.ACTIVE):
        return RegistrationComparison.RECOVERY_IN_PROGRESS
    if anchor.retired_generations != database.retired_generations:
        return RegistrationComparison.HISTORY_MISMATCH
    return RegistrationComparison.MATCHING_ACTIVE_DATA


class Health(Enum):
    HEALTHY = "HEALTHY"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class RequestOutcome(Enum):
    NOT_OBSERVED = "NOT_OBSERVED"
    RECORDED = "RECORDED"
    TERMINAL = "TERMINAL"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True, slots=True)
class LifecycleStatus:
    """Diagnostic observations only; never evidence of approval/completion."""

    health: Health
    outcome: RequestOutcome = RequestOutcome.NOT_OBSERVED

    def __post_init__(self):
        if type(self.health) is not Health or type(self.outcome) is not RequestOutcome:
            raise ValueError("typed lifecycle observations required")

    def observe_health(self, health):
        return replace(self, health=health)

    def observe_outcome(self, outcome):
        if type(outcome) is not RequestOutcome:
            raise ValueError("typed outcome required")
        # A later health/query success cannot settle an ambiguous attempt.
        # Reconciliation needs a separate reviewed evidence contract.
        if self.outcome is RequestOutcome.OUTCOME_UNKNOWN:
            return self
        return replace(self, outcome=outcome)


class BudgetExceeded(TimeoutError):
    pass


def _timestamp(value):
    if type(value) not in (int, float):
        raise ValueError("finite nonnegative timestamp required")
    try:
        valid = math.isfinite(value) and 0 <= value <= 10**12
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("finite nonnegative timestamp required")
    return value


@dataclass(frozen=True, slots=True)
class ObservationBudget:
    """Fixed start/timeout and count; no waits, callbacks or clock access.

    Keep the latest returned instance. A failed reservation must end the
    caller's observation loop. This immutable model cannot enforce caller
    cooperation, cap an OS call's duration or manage pending I/O lifetimes.
    """

    started_at: float
    timeout_seconds: float = 15.0
    maximum_observations: int = 30
    observations: int = 0
    last_observed_at: float | None = None

    def __post_init__(self):
        _timestamp(self.started_at)
        _timestamp(self.timeout_seconds)
        if not 0 < self.timeout_seconds <= 3600:
            raise ValueError("timeout outside bounded range")
        if (type(self.maximum_observations) is not int
                or not 1 <= self.maximum_observations <= 1000
                or type(self.observations) is not int
                or not 0 <= self.observations <= self.maximum_observations):
            raise ValueError("invalid observation count")
        if (self.observations == 0) != (self.last_observed_at is None):
            raise ValueError("observation timestamp/count mismatch")
        if self.last_observed_at is not None:
            if _timestamp(self.last_observed_at) < self.started_at:
                raise ValueError("observation precedes start")
            self._elapsed(self.last_observed_at)
            if self.last_observed_at - self.started_at >= self.timeout_seconds:
                raise ValueError("recorded observation past deadline")

    def _elapsed(self, now):
        _timestamp(now)
        lower = (self.started_at if self.last_observed_at is None
                 else self.last_observed_at)
        if now < lower:
            raise ValueError("monotonic timestamp moved backwards")
        return now - self.started_at

    def remaining_seconds(self, now):
        elapsed = self._elapsed(now)
        if self.observations == self.maximum_observations:
            return 0.0
        return max(0.0, self.timeout_seconds - elapsed)

    def reserve_observation(self, now):
        """Return a charged data snapshot, BEFORE the caller does any work."""
        elapsed = self._elapsed(now)
        if elapsed >= self.timeout_seconds:
            raise BudgetExceeded("observation deadline exceeded")
        if self.observations >= self.maximum_observations:
            raise BudgetExceeded("observation count exhausted")
        return replace(self, observations=self.observations + 1,
                       last_observed_at=now)
