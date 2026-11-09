"""DEV_ONLY synthetic session correlation. Never proves cryptographic/OS identity."""
from dataclasses import dataclass

class EvidenceError(ValueError):
    pass

def _hex(v, n):
    if type(v) is not str or len(v) != n or any(c not in "0123456789abcdef" for c in v):
        raise EvidenceError("invalid canonical identifier")

@dataclass(frozen=True, slots=True)
class Expected:
    installation: str
    generation: str
    owner: str
    epoch: str
    direction: str
    next_sequence: int

    def __post_init__(self):
        for field in (self.installation, self.generation, self.epoch):
            _hex(field, 32)
        if type(self.owner) is not str or not self.owner.startswith("S-1-5-") or len(self.owner) > 128:
            raise EvidenceError("invalid owner label")
        if self.direction not in ("CLIENT_TO_SERVICE", "SERVICE_TO_CLIENT"):
            raise EvidenceError("invalid direction")
        if type(self.next_sequence) is not int or not 0 <= self.next_sequence < 2**63:
            raise EvidenceError("invalid sequence")

@dataclass(frozen=True, slots=True)
class ClaimedMessage:
    installation: str
    generation: str
    owner: str
    epoch: str
    direction: str
    sequence: int
    claimed_authenticated: bool = False

def inspect_data_only(expected, message):
    """Return (matching_data, authority, admission); never authenticate."""
    if type(expected) is not Expected or type(message) is not ClaimedMessage:
        raise EvidenceError("exact records required")
    expected.__post_init__()
    if type(message.sequence) is not int or not 0 <= message.sequence < 2**63:
        raise EvidenceError("invalid message sequence")
    if type(message.claimed_authenticated) is not bool:
        raise EvidenceError("invalid claim type")
    matched = (message.installation == expected.installation
               and message.generation == expected.generation
               and message.owner == expected.owner
               and message.epoch == expected.epoch
               and message.direction == expected.direction
               and message.sequence == expected.next_sequence)
    return (matched, "NOT_AUTHENTICATED", "UNAVAILABLE")
