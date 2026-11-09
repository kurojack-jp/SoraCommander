"""Phase 9K: in-memory simulation of A/B release slot transitions.

No code execution, network, OS ACL, Task, service or production install.
Only a receipt already independently inspected by Phase9J may be staged.
Neither this simulation nor its tests provide a protected trust anchor.
"""
from dataclasses import dataclass
import json
from pathlib import Path

class SlotSimulationError(ValueError):
    pass

@dataclass(frozen=True)
class SlotSnapshot:
    active: str
    sequence: int
    previous: str | None
    pending: str | None
    high_water: int
    status: str
    production_authorized: bool = False
    remote_authorized: bool = False

class LocalSlotSimulator:
    """Pure offline A/B transition logic; caller stores its own state."""
    def __init__(self, *, active="A", sequence=1, high_water=None):
        if active not in ("A", "B") or type(sequence) is not int or sequence < 1:
            raise SlotSimulationError("invalid initial slot")
        if high_water is None:
            high_water = sequence
        if type(high_water) is not int or high_water < sequence:
            raise SlotSimulationError("invalid release floor")
        self.active = active
        self.sequence = sequence
        self.high_water = high_water
        self.previous = None
        self.pending = None
        self.pending_sequence = None
        self.previous_sequence = None
        self.status = "STABLE"

    def snapshot(self):
        return SlotSnapshot(self.active,self.sequence,self.previous,self.pending,
                            self.high_water,self.status)

    def stage(self, preview):
        if self.status != "STABLE":
            raise SlotSimulationError("unfinished update")
        if (getattr(preview,"status",None) != "SIGNED_UPDATE_VALIDATED_INSTALL_AND_ACTIVATION_HOLD"
                or type(getattr(preview,"from_sequence",None)) is not int
                or preview.from_sequence != self.sequence
                or type(getattr(preview,"to_sequence",None)) is not int
                or preview.to_sequence <= self.high_water
                or getattr(preview,"installation_permitted",None) is not False
                or getattr(preview,"activation_permitted",None) is not False
                or getattr(preview,"remote_permitted",None) is not False):
            raise SlotSimulationError("candidate not eligible for offline simulation")
        self.pending = "B" if self.active == "A" else "A"
        self.pending_sequence = preview.to_sequence
        self.status = "STAGED_SIMULATION"
        return self.snapshot()

    def simulate_activation(self, *, owner_ack=False):
        if self.status != "STAGED_SIMULATION" or owner_ack is not True:
            raise SlotSimulationError("explicit simulated owner acknowledgement required")
        self.previous, self.previous_sequence = self.active, self.sequence
        self.active, self.sequence = self.pending, self.pending_sequence
        self.high_water = max(self.high_water,self.sequence)
        self.pending, self.pending_sequence = None,None
        self.status = "AWAITING_FAKE_HEALTH"
        return self.snapshot()

    def simulate_health(self, *, healthy):
        if self.status != "AWAITING_FAKE_HEALTH" or type(healthy) is not bool:
            raise SlotSimulationError("simulated health result required")
        if healthy:
            self.previous,self.previous_sequence = None,None
            self.status = "STABLE"
        else:
            # Recovery of already retained *verified* slot only.
            self.active,self.sequence = self.previous,self.previous_sequence
            self.previous,self.previous_sequence = None,None
            self.status = "RECOVERED_OLD_SLOT_SIMULATION"
            # High-water is intentionally NOT reduced by recovery.
        return self.snapshot()

    def reset_after_recovery(self):
        if self.status != "RECOVERED_OLD_SLOT_SIMULATION":
            raise SlotSimulationError("not recovering")
        self.status = "STABLE"
        return self.snapshot()

    def abort_pending(self):
        if self.status != "STAGED_SIMULATION":
            raise SlotSimulationError("no staged candidate")
        self.pending,self.pending_sequence = None,None
        self.status = "STABLE"
        return self.snapshot()
