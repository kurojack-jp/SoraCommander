"""DATA_ONLY offline intent model: never grants execute or verifies Owner.

No storage, I/O, clock, process, GUI or cryptographic authority. Identifiers and
command_binding are externally supplied, untrusted data, never capabilities.
request_id identifies one attempt; operation_id identifies the intended operation.
A future Broker must bind the semantic command independently. A legitimate repeat
needs a distinct operation_id AND a separate trusted Owner authorization contract,
which cannot be represented here. No production runtime integration is provided.

Snapshots simulate reconstruction only, not durable storage or restart-safe runtime.
Schema validation cannot detect coherent forgery, stale copies or rollback to NEW.
Callers must retain the returned aggregate; copies do not enforce global exclusion.
"""
from dataclasses import dataclass, replace
from enum import Enum
import re


class IntentState(Enum):
    NEW = "NEW"
    RESERVED_UNKNOWN = "RESERVED_UNKNOWN"
    CONFIRMED_TERMINAL = "CONFIRMED_TERMINAL"
    UNKNOWN_HELD = "UNKNOWN_HELD"


class IntentHeld(ValueError):
    """HOLD: another reservation is forbidden; existing evidence is unchanged."""


def _hex(value, length):
    if (type(value) is not str or len(value) != length
            or re.fullmatch(r"[0-9a-f]+", value) is None):
        raise ValueError("bounded lowercase hex identifier required")
    return value


@dataclass(frozen=True, slots=True)
class OperationIntent:
    """Immutable observations only; construction/reconstruction confers no trust."""

    operation_id: str
    command_binding: str
    state: IntentState = IntentState.NEW
    request_id: str | None = None

    def __post_init__(self):
        _hex(self.operation_id, 32)
        _hex(self.command_binding, 64)
        if type(self.state) is not IntentState:
            raise ValueError("typed intent state required")
        if self.state is IntentState.NEW:
            if self.request_id is not None:
                raise ValueError("new intent cannot contain an attempt")
        else:
            _hex(self.request_id, 32)

    def reserve_data_only(self, request_id):
        """Record one request before any hypothetical spawn; never authorize it."""
        _hex(request_id, 32)
        if self.state is not IntentState.NEW:
            raise IntentHeld("HOLD_OPERATION_ALREADY_RESERVED_DATA_ONLY")
        return replace(self, state=IntentState.RESERVED_UNKNOWN,
                       request_id=request_id)

    def mark_unknown_data_only(self):
        """Retain ambiguous attempt evidence; repeated UNKNOWN is idempotent."""
        if self.state is IntentState.UNKNOWN_HELD:
            return self
        if self.state is not IntentState.RESERVED_UNKNOWN:
            raise ValueError("invalid unknown observation transition")
        return replace(self, state=IntentState.UNKNOWN_HELD)

    def mark_terminal_data_only(self):
        """Caller must independently verify terminal evidence before calling.

        This model neither receives nor verifies that evidence. The recorded
        observation never permits reuse, even after reconstruction.
        """
        if self.state not in (IntentState.RESERVED_UNKNOWN, IntentState.UNKNOWN_HELD):
            raise ValueError("invalid terminal observation transition")
        return replace(self, state=IntentState.CONFIRMED_TERMINAL)

    def inspect_data_only(self):
        """Return diagnostic status only, never permission or an Owner UI token."""
        return {
            IntentState.NEW: "NEW_INERT_DATA_ONLY",
            IntentState.RESERVED_UNKNOWN: "HOLD_RESERVED_UNKNOWN_DATA_ONLY",
            IntentState.UNKNOWN_HELD: "HOLD_UNKNOWN_DATA_ONLY",
            IntentState.CONFIRMED_TERMINAL: "HOLD_TERMINAL_NO_REUSE_DATA_ONLY",
        }[self.state]

    def serialize(self):
        """Detached JSON-safe string mapping; empty request_id means no attempt."""
        return {"version": "1", "operation_id": self.operation_id,
                "command_binding": self.command_binding, "state": self.state.value,
                "request_id": "" if self.request_id is None else self.request_id}

    @classmethod
    def from_data_only_snapshot(cls, snapshot):
        """Reject malformed schema/invariants, without authenticating snapshots."""
        keys = {"version", "operation_id", "command_binding", "state", "request_id"}
        if (type(snapshot) is not dict or len(snapshot) != len(keys)
                or any(type(key) is not str or key not in keys for key in snapshot)
                or any(type(value) is not str or len(value) > 64
                       for value in snapshot.values())
                or snapshot["version"] != "1"):
            raise ValueError("invalid data-only snapshot schema")
        try:
            state = IntentState(snapshot["state"])
        except ValueError:
            raise ValueError("invalid data-only snapshot state") from None
        return cls(snapshot["operation_id"], snapshot["command_binding"], state,
                   None if snapshot["request_id"] == "" else snapshot["request_id"])
