"""B04 DEV_ONLY join of existing synthetic SCM/process/token observations.

This intentionally cannot authenticate OS identities, independent installer
trust or real ncalrpc peers. No native calls. No product authority.
"""
from dataclasses import dataclass, field

from sora_commander_next.offline_b04_service_identity_probe import ServiceProcessObservation
from sora_commander_next.service_auth_candidate import (
    ReleasePolicyCandidate, compare_authentication_prerequisites,
)
from sora_commander_next.service_native_prerequisites import NativePrerequisites
from sora_commander_next.service_scm_retained_bridge_candidate import RetainedScmProcessCandidate

_MISSING = (
    "RELEASE_PIN_MUST_ORIGINATE_FROM_PROTECTED_INSTALLER",
    "WINVERIFYTRUST_PUBLISHER_AND_MAPPED_IMAGE_NOT_PROVEN",
    "SCM_TO_NATIVE_SAMPLES_NOT_PROVEN_SAME_RETAINED_HANDLE",
    "LIVE_RPC_PEER_NOT_BOUND_TO_SERVICE_PROCESS",
    "RPC_SERVICE_SID_NOT_VERIFIED_BY_OS",
    "RPC_CONNECTION_EPOCH_AND_REPLY_LIFETIME_NOT_VERIFIED",
    "B01_PROTECTED_COMMIT_AND_OWNER_APPROVAL_NOT_VERIFIED",
)


@dataclass(frozen=True, slots=True)
class UntrustedRpcPeerClaim:
    service_name: str
    pid: int
    created_filetime: int
    epoch: int
    installation_id: str
    release_generation: str
    service_sid: str
    image_path: str
    image_sha256: str
    # A peer can lie about these and all other fields. Never trust them.
    claimed_authenticated: bool = False
    claimed_signed: bool = False


@dataclass(frozen=True, slots=True, init=False)
class OfflineProvenanceJoin:
    data_matches: bool
    mismatches: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    authentication: str = field(default="NOT_AUTHENTICATED", init=False)
    admission: str = field(default="UNAVAILABLE", init=False)
    os_acceptance: str = field(default="NOT_OS_ACCEPTED", init=False)
    spawn_allowed: bool = field(default=False, init=False)

    def __init__(self, data_matches: bool, mismatches: tuple[str, ...],
                 missing_evidence: tuple[str, ...]):
        object.__setattr__(self, "data_matches", data_matches)
        object.__setattr__(self, "mismatches", mismatches)
        object.__setattr__(self, "missing_evidence", missing_evidence)
        object.__setattr__(self, "authentication", "NOT_AUTHENTICATED")
        object.__setattr__(self, "admission", "UNAVAILABLE")
        object.__setattr__(self, "os_acceptance", "NOT_OS_ACCEPTED")
        object.__setattr__(self, "spawn_allowed", False)


def _valid_process_record(p) -> bool:
    """Strict synthetic process record validation (not OS provenance)."""
    return (
        type(p) is ServiceProcessObservation
        and type(p.service_name) is str and p.service_name != ""
        and type(p.pid) is int and 0 < p.pid <= 0xFFFFFFFF
        and type(p.created_filetime) is int and 0 < p.created_filetime < 2**64
        and type(p.started_tick_ns) is int
        and type(p.finished_tick_ns) is int
        and 0 <= p.started_tick_ns <= p.finished_tick_ns
    )


def join_untrusted_provenance(policy, retained, native, peer) -> OfflineProvenanceJoin:
    """Detect contradictions in synthetic records, never grant OS authority."""
    if (type(policy) is not ReleasePolicyCandidate
            or type(retained) is not RetainedScmProcessCandidate
            or type(native) is not NativePrerequisites
            or type(peer) is not UntrustedRpcPeerClaim
            or type(retained.process) is not ServiceProcessObservation
            or type(native.process) is not ServiceProcessObservation):
        raise TypeError("EXACT_DEV_ONLY_RECORDS_REQUIRED")

    candidate = compare_authentication_prerequisites(
        policy, retained.initial, retained.final, native)
    errors = list(candidate.mismatches)
    if not _valid_process_record(retained.process):
        errors.append("INVALID_SCM_PROCESS_RECORD")
    if not _valid_process_record(native.process):
        errors.append("INVALID_NATIVE_PROCESS_RECORD")

    # SCM-retained process observation and independently supplied native
    # snapshot need a data join; the old comparator did not accept SCM record.
    if retained.process != native.process:
        errors.append("SCM_NATIVE_PROCESS_SNAPSHOTS_MISMATCH")

    expected_service = retained.process.service_name
    if type(peer.service_name) is not str or peer.service_name != expected_service:
        errors.append("UNTRUSTED_RPC_SERVICE_NAME_MISMATCH")

    if (type(peer.pid) is not int or peer.pid != retained.process.pid
            or type(peer.created_filetime) is not int
            or peer.created_filetime != retained.process.created_filetime):
        errors.append("UNTRUSTED_RPC_PID_OR_BIRTH_MISMATCH")

    if (type(peer.epoch) is not int or peer.epoch != native.connection_epoch
            or peer.epoch <= 0):
        errors.append("UNTRUSTED_RPC_EPOCH_MISMATCH")

    for field, value in (
        ("installation_id", policy.installation_id),
        ("release_generation", policy.generation),
        ("service_sid", policy.service_sid),
        ("image_path", policy.image_path),
        ("image_sha256", policy.image_sha256),
    ):
        actual = getattr(peer, field)
        if type(actual) is not str or actual != value:
            errors.append("UNTRUSTED_RPC_" + field.upper() + "_MISMATCH")

    # The existing comparator has no process mapped-image release hash.
    # Matching a SHA string asserted by a peer is not image verification.
    return OfflineProvenanceJoin(
        not errors, tuple(dict.fromkeys(errors)),
        _MISSING + tuple(candidate.missing_evidence),
    )
