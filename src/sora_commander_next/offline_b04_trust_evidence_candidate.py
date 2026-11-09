"""DEV_ONLY B04 independent trust evidence gate. Never authorizes work.

Caller-supplied observations are not authority, even if internally consistent.
This review object is intentionally always NOT_AUTHENTICATED.
"""
from dataclasses import dataclass
import re

HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")


class TrustReviewDenied(ValueError):
    pass


@dataclass(frozen=True)
class B04TrustEvidence:
    publisher_spki_sha256: str
    installer_sha256: str
    release_sha256: str
    installed_service_sha256: str
    installation_id: str
    generation: int
    connected_pipe_epoch: int
    observer_pipe_epoch: int
    trusted_installer_origin: bool
    protected_anchor_observed: bool
    native_peer_verified: bool

    def __post_init__(self):
        for v in (self.publisher_spki_sha256, self.installer_sha256,
                  self.release_sha256, self.installed_service_sha256):
            if type(v) is not str or HEX64.fullmatch(v) is None:
                raise TrustReviewDenied("MALFORMED_HASH")
        if type(self.installation_id) is not str or HEX32.fullmatch(self.installation_id) is None:
            raise TrustReviewDenied("MALFORMED_INSTALLATION")
        if (type(self.generation) is not int or self.generation < 1
                or type(self.connected_pipe_epoch) is not int or self.connected_pipe_epoch < 1
                or type(self.observer_pipe_epoch) is not int or self.observer_pipe_epoch < 1):
            raise TrustReviewDenied("INVALID_GENERATION_OR_EPOCH")
        for v in (self.trusted_installer_origin, self.protected_anchor_observed,
                  self.native_peer_verified):
            if type(v) is not bool:
                raise TrustReviewDenied("INVALID_EVIDENCE_TYPE")


@dataclass(frozen=True)
class B04TrustReview:
    evidence_complete: bool
    pipe_epoch_consistent: bool
    authentication: str = "NOT_AUTHENTICATED"
    admission: str = "UNAVAILABLE"


def review_untrusted_evidence(item: B04TrustEvidence) -> B04TrustReview:
    if type(item) is not B04TrustEvidence:
        raise TrustReviewDenied("INVALID_EVIDENCE")
    complete = (item.trusted_installer_origin
                and item.protected_anchor_observed and item.native_peer_verified)
    return B04TrustReview(bool(complete), item.connected_pipe_epoch == item.observer_pipe_epoch)
