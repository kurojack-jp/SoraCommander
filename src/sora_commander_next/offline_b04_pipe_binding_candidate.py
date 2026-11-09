"""DEV_ONLY: observational binding of a signed transcript to a pipe lifetime.

No OS identity authentication, no IPC access, no admission.
"""
from dataclasses import dataclass
import re

HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class PipeBindingDenied(ValueError):
    pass


@dataclass(frozen=True)
class PipeBindingObservation:
    connected_handle_identity: str
    transcript_handle_identity: str
    connection_epoch: int
    transcript_epoch: int
    owner_session_id: int
    transcript_owner_session_id: int
    client_nonce: str
    transcript_client_nonce: str
    server_nonce: str
    transcript_server_nonce: str

    def __post_init__(self):
        for v in (self.connected_handle_identity, self.transcript_handle_identity):
            if type(v) is not str or not v or len(v) > 128:
                raise PipeBindingDenied("INVALID_OBSERVED_HANDLE")
        for v in (self.client_nonce, self.transcript_client_nonce,
                  self.server_nonce, self.transcript_server_nonce):
            if type(v) is not str or HEX64.fullmatch(v) is None:
                raise PipeBindingDenied("INVALID_NONCE")
        for v in (self.connection_epoch, self.transcript_epoch):
            if type(v) is not int or not 0 < v < (1 << 63):
                raise PipeBindingDenied("INVALID_EPOCH")
        for v in (self.owner_session_id, self.transcript_owner_session_id):
            if type(v) is not int or not 0 <= v < (1 << 32):
                raise PipeBindingDenied("INVALID_SESSION")


@dataclass(frozen=True)
class PipeBindingReview:
    observations_match: bool
    authentication: str = "NOT_AUTHENTICATED"
    admission: str = "UNAVAILABLE"


def review_pipe_binding(observation: PipeBindingObservation) -> PipeBindingReview:
    if type(observation) is not PipeBindingObservation:
        raise PipeBindingDenied("INVALID_OBSERVATION")
    matches = (
        observation.connected_handle_identity == observation.transcript_handle_identity
        and observation.connection_epoch == observation.transcript_epoch
        and observation.owner_session_id == observation.transcript_owner_session_id
        and observation.client_nonce == observation.transcript_client_nonce
        and observation.server_nonce == observation.transcript_server_nonce
        and observation.client_nonce != observation.server_nonce
    )
    return PipeBindingReview(matches)
