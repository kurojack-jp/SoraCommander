"""Phase9Y: pure independent release trust anchor and high-water review.

No keys are created and no protected state is read/written.
"""
from dataclasses import dataclass, field
import re

HEX=re.compile(r"[0-9a-f]{64}")
class AnchorReviewError(ValueError):
    pass

@dataclass(frozen=True)
class AnchorReview:
    key_fingerprint: str
    protected_floor: int
    # These are observations, never caller-selected authority. This prevents
    # accidental construction of an apparently authorized review; it is not
    # a security boundary against code in the same Python process.
    status: str = field(default="OWNER_OUT_OF_BAND_EVIDENCE_REQUIRED", init=False)
    deploy_allowed: bool = field(default=False, init=False)
    remote_allowed: bool = field(default=False, init=False)
    journal_write_allowed: bool = field(default=False, init=False)

def review_anchor(*, publisher_fingerprint, owner_confirmed_fingerprint,
                  protected_floor, observed_floor, proposed_sequence):
    values=(publisher_fingerprint,owner_confirmed_fingerprint)
    if any(type(x) is not str or HEX.fullmatch(x) is None for x in values):
        raise AnchorReviewError("valid fingerprint strings required")
    if values[0]!=values[1]:
        raise AnchorReviewError("owner-pinned publisher fingerprint mismatch")
    if any(type(v) is not int or not 0<=v<2**63 for v in
           (protected_floor,observed_floor,proposed_sequence)):
        raise AnchorReviewError("invalid version counters")
    if observed_floor < protected_floor:
        raise AnchorReviewError("observed journal was rolled back")
    if proposed_sequence <= max(protected_floor,observed_floor):
        raise AnchorReviewError("release is not strictly newer")
    return AnchorReview(publisher_fingerprint,protected_floor)
