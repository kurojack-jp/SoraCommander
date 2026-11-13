"""Shared immutable C3/C4 review proof data; no UI, authority or writer imports."""
from dataclasses import dataclass


@dataclass(frozen=True)
class _VerifiedFileReviewExpiry:
    """Local invocation-bound proof emitted only before either writer is entered."""
    review_token: object
    path: str
    old_sha256: str | None
    new_sha256: str
    operation: str
    write_outcome: str = 'NOT_STARTED'
    reason: str = 'EXPIRED'
    deadline: float | None = None
    timer_at: float | None = None

