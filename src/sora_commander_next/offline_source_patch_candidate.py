"""DEV_ONLY pure source-patch admission evidence. No file I/O or write authority."""
from dataclasses import dataclass
from hashlib import sha256

@dataclass(frozen=True)
class PatchCandidate:
    old_sha256: str
    new_sha256: str
    bytes_before: int
    bytes_after: int
    changed: bool
    authority: str = "NONE_PREVIEW_ONLY"

def inspect_patch_bytes(before: bytes, after: bytes, expected_old_sha256: str) -> PatchCandidate:
    """Fail closed on invalid/stale/unbounded data; NEVER authorize a write."""
    if type(before) is not bytes or type(after) is not bytes:
        raise TypeError("exact bytes required")
    if len(before) > 16384 or len(after) > 16384 or b"\x00" in before or b"\x00" in after:
        raise ValueError("unsupported source bytes")
    before.decode("utf-8", errors="strict")
    after.decode("utf-8", errors="strict")
    if type(expected_old_sha256) is not str or len(expected_old_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_old_sha256):
        raise ValueError("invalid expected digest")
    current = sha256(before).hexdigest()
    if current != expected_old_sha256:
        raise ValueError("stale source")
    return PatchCandidate(current, sha256(after).hexdigest(), len(before), len(after), before != after)
