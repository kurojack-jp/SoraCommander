"""Phase9X: bounded read-only interpreter binary attestation evidence.

Local SHA-256 is an observation, not independent trusted provenance.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sys

class RuntimeAttestationError(ValueError):
    pass

@dataclass(frozen=True)
class RuntimeBinaryEvidence:
    path: str
    sha256: str
    bytes: int
    externally_authenticated: bool = False
    install_authorized: bool = False

def inspect_binary(path, *, max_bytes=100_000_000):
    p=Path(path)
    if not p.is_absolute() or p.is_symlink() or (hasattr(p,"is_junction") and p.is_junction()):
        raise RuntimeAttestationError("absolute regular unlinked binary required")
    if p.suffix.lower() not in (".exe",".dll"):
        raise RuntimeAttestationError("expected executable or DLL")
    try:
        size=p.stat().st_size
        if not 0<size<=max_bytes or not p.is_file():
            raise RuntimeAttestationError("invalid size or path")
        digest=hashlib.sha256()
        with p.open("rb") as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeAttestationError("unreadable binary") from exc
    return RuntimeBinaryEvidence(str(p),digest.hexdigest(),size)

def review_binary(evidence, *, externally_pinned_sha256):
    if (type(evidence) is not RuntimeBinaryEvidence or
        type(externally_pinned_sha256) is not str or
        len(externally_pinned_sha256)!=64 or
        any(c not in "0123456789abcdef" for c in externally_pinned_sha256)):
        raise RuntimeAttestationError("invalid external pin")
    if evidence.sha256 != externally_pinned_sha256:
        raise RuntimeAttestationError("binary digest mismatch")
    return {"digest_matches_supplied_pin":True,
            "external_pin_origin_verified":False,
            "authenticode_verified":False,
            "installation_permitted":False}
