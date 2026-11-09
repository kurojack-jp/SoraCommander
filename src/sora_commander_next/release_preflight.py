"""Phase 9H offline preflight: independently pinned candidate check only.

A successful PREVIEW never authorizes installation, launch, approval, Task,
Remote MCP or a network listener. No token, auth secret, signature issuance,
ACL modification, or privileged action is performed by this module.
"""
from dataclasses import dataclass, field
from pathlib import Path
import os
import stat

from .release_inventory import verify_candidate, CandidateIntegrityError
from .release_layout import (
    ReleaseLayout, ReleaseLayoutError, layout_specification
)

@dataclass(frozen=True)
class OfflinePreflight:
    status: str
    content_verified: bool
    deployment_authorized: bool = field(default=False, init=False)
    remote_authorized: bool = field(default=False, init=False)
    trust_limit: str
    pending_reviews: tuple[str, ...]

    @property
    def launch_permitted(self):
        return False

def _check_ancestor_links(location):
    """Reject links/junctions at any existing ancestor of the staging root."""
    p = Path(location)
    if not p.is_absolute():
        raise CandidateIntegrityError("absolute candidate location required")
    for item in (p, *p.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise CandidateIntegrityError("link or junction in staging ancestors")
    if not p.is_dir():
        raise CandidateIntegrityError("staging root unavailable")

def inspect_offline_candidate_with_wheelhouse(
        candidate_root, *, expected_manifest_sha256, layout,
        wheelhouse_root, expected_wheels, target_python="cp313",
        target_platform="win_amd64"):
    """Report BOTH candidate and wheel completeness, never authorize install.

    No implicit download, wheel installation, protected-path write, or trust
    promotion; both pins are caller-provided and not authenticated here.
    """
    from .release_wheelhouse import verify_required_wheelhouse
    candidate = inspect_offline_candidate(
        candidate_root, expected_manifest_sha256=expected_manifest_sha256,
        layout=layout)
    wheels = verify_required_wheelhouse(
        wheelhouse_root, expected_wheels, target_python=target_python,
        target_platform=target_platform)
    return {
        "status": "OFFLINE_CONTENT_PREVIEW_DEPLOYMENT_HOLD",
        "candidate_verified": candidate.content_verified,
        "wheel_inventory_verified": wheels["hashes_match_supplied_inventory"],
        "required_distributions_present": wheels["required_present"],
        "target_python": wheels["target_python"],
        "target_platform": wheels["target_platform"],
        "minimum_pywin32": wheels["minimum_pywin32"],
        "deployment_authorized": False,
        "remote_authorized": False,
        "launch_permitted": False,
        "publisher_authenticated": False,
        "trust_limit": (
            "Caller-supplied manifest/wheel hashes are not independently trusted; "
            "wheel metadata, transitive requirements and installation remain unverified"
        ),
        "remaining_reviews": candidate.pending_reviews,
    }


def inspect_offline_candidate(candidate_root, *, expected_manifest_sha256, layout):
    """Verify bounded bytes and output a DATA-ONLY HOLD report.

    The trusted pin's provenance and verifier's integrity are unproven here.
    The caller must obtain those protections independently before deployment.
    """
    if type(layout) is not ReleaseLayout:
        raise ReleaseLayoutError("explicit reviewed Windows layout required")
    spec = layout_specification(layout)
    _check_ancestor_links(candidate_root)
    verify_candidate(candidate_root, trusted_manifest_sha256=expected_manifest_sha256)
    return OfflinePreflight(
        status="OFFLINE_CANDIDATE_VERIFIED_DEPLOYMENT_HOLD",
        content_verified=True,
        trust_limit="Caller-provided digest is NOT authenticated by this verifier",
        pending_reviews=tuple(spec["required_checks"]),
    )
