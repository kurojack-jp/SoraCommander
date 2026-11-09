"""DEV_ONLY composite review of untrusted pipe binding and HMAC sequence evidence.

Even perfect observations do not establish Windows peer authenticity or permission.
"""
from dataclasses import dataclass
from .offline_b04_pipe_binding_candidate import PipeBindingObservation, review_pipe_binding
from .offline_b04_trust_evidence_candidate import B04TrustEvidence, review_untrusted_evidence
from .offline_b04_message_integrity_candidate import OfflineSequenceReview, MessageEnvelope


@dataclass(frozen=True)
class CompositeReview:
    binding_observations_match: bool
    trust_observations_complete: bool
    mac_matches: bool
    sequence_next: bool
    authentication: str = "NOT_AUTHENTICATED"
    admission: str = "UNAVAILABLE"


def review_composite(binding: PipeBindingObservation, trust: B04TrustEvidence,
                     tracker: OfflineSequenceReview, envelope: MessageEnvelope,
                     demo_key: bytes, supplied_mac: bytes) -> CompositeReview:
    if type(tracker) is not OfflineSequenceReview:
        raise TypeError("INVALID_SEQUENCE_REVIEW")
    b = review_pipe_binding(binding)
    t = review_untrusted_evidence(trust)
    # Avoid consuming an in-memory sequence when other observations conflict.
    aligned = (
        b.observations_match and t.evidence_complete and t.pipe_epoch_consistent
        and binding.connection_epoch == trust.connected_pipe_epoch == envelope.epoch
        and binding.client_nonce == envelope.client_nonce
        and binding.server_nonce == envelope.server_nonce
        and binding.connection_epoch == tracker.epoch
        and binding.client_nonce == tracker.client_nonce
        and binding.server_nonce == tracker.server_nonce
    )
    if not aligned:
        return CompositeReview(b.observations_match, t.evidence_complete, False, False)
    m = tracker.review(demo_key, envelope, supplied_mac)
    return CompositeReview(True, True, m.mac_matches, m.next_sequence)
