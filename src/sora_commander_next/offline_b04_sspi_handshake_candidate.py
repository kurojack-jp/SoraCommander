"""B04 DEV_ONLY SSPI two-sided handshake evidence contract (NO native SSPI).

A *mock observation* from InitializeSecurityContext / AcceptSecurityContext
may be validated here, but never becomes a trusted service identity or a
permission to call protected ledger / launch. The output is DATA_ONLY.

No Secur32, WinDLL, credential acquisition, pipe, file, socket, SQLite, Owner
approval or native child job code. All inputs (even final-success attributes
and the reported package) are UNTRUSTED until independently OS-verified.
"""
from dataclasses import dataclass, field, asdict
from hashlib import sha256
import json
from threading import get_ident
from time import monotonic_ns
import re

MAX_STEPS = 12
MAX_TOKEN_BYTES = 65536
MAX_TIMEOUT_MS = 5000
DOMAIN = b"scn-b04/sspi-handshake-candidate-v1\x00"
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
REQUIRED_FINAL_ATTRIBUTES = frozenset((
    "MUTUAL_AUTH", "INTEGRITY", "REPLAY_DETECT", "SEQUENCE_DETECT",
))
ALLOWED_ATTRIBUTES = REQUIRED_FINAL_ATTRIBUTES | frozenset(("CONFIDENTIALITY",))
# A future independent acceptance must verify package negotiation, local SPN,
# publisher/install generation and verified peer principal on BOTH ends.
REVIEWED_NEGOTIATED_PACKAGE = "Kerberos"


class SspiCandidateDenied(ValueError):
    def __init__(self, code):
        super().__init__(code + ": NOT_AUTHENTICATED / UNAVAILABLE")
        self.code = code


def _text(value, maximum=256):
    if (type(value) is not str or not value or len(value) > maximum
            or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise SspiCandidateDenied("INVALID_PINNED_PRINCIPAL")
    return value


def _hex(value, pattern, code):
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise SspiCandidateDenied(code)
    return value


@dataclass(frozen=True, slots=True)
class SspiCandidatePins:
    installation_id: str
    generation: str
    broker_instance_id: str
    connection_epoch: int
    client_nonce: str
    server_nonce: str
    service_principal: str
    owner_principal: str

    def __post_init__(self):
        for value in (self.installation_id, self.generation, self.broker_instance_id):
            _hex(value, HEX32, "INVALID_INSTALLATION_GENERATION_OR_BROKER")
        for value in (self.client_nonce, self.server_nonce):
            _hex(value, HEX64, "INVALID_HANDSHAKE_NONCE")
        if self.client_nonce == self.server_nonce:
            raise SspiCandidateDenied("NONCE_COLLISION")
        if type(self.connection_epoch) is not int or not 0 < self.connection_epoch < 1 << 63:
            raise SspiCandidateDenied("INVALID_CONNECTION_EPOCH")
        _text(self.service_principal)
        _text(self.owner_principal)
        if self.service_principal == self.owner_principal:
            raise SspiCandidateDenied("PRINCIPALS_NOT_DISTINCT")


@dataclass(frozen=True, slots=True)
class SspiStepCandidate:
    role: str                         # CLIENT(Initialize) or SERVER(Accept)
    ordinal: int                      # 0-based conversation order
    outcome: str                      # CONTINUE or OK (final SEC_E_OK)
    output_token: bytes               # opaque handshake token; no interpretation
    returned_attributes: frozenset     # final native ISC_RET/ASC_RET mapping
    selected_package: str             # claimed QueryContextAttributes result
    observed_peer_principal: str      # claimed QueryContextAttributes result
    installation_id: str
    generation: str
    broker_instance_id: str
    connection_epoch: int
    client_nonce: str
    server_nonce: str


@dataclass(frozen=True, slots=True)
class SspiDataOnlyReview:
    transcript_sha256: str
    steps: int
    client_final: bool
    server_final: bool
    package_observation: str
    output_state: str = field(default="MOCK_CONTRACT_COMPLETE", init=False)
    authentication: str = field(default="NOT_AUTHENTICATED", init=False)
    admission: str = field(default="UNAVAILABLE", init=False)
    service_identity_os_verified: bool = field(default=False, init=False)
    owner_identity_os_verified: bool = field(default=False, init=False)
    signature_verified_on_connected_handle: bool = field(default=False, init=False)
    native_sspi_context_bound: bool = field(default=False, init=False)
    independent_publisher_anchor_verified: bool = field(default=False, init=False)
    live_replay_protected: bool = field(default=False, init=False)
    missing_evidence: tuple = field(default=(
        "NATIVE_SSPI_CONTEXT_AND_CREDENTIALS_NOT_ACCEPTED",
        "QUERY_CONTEXT_ATTRIBUTES_OS_RESULTS_NOT_PROVEN",
        "SERVICE_PRINCIPAL_SPNS_AND_ACCOUNT_MAPPING_NOT_OS_ACCEPTED",
        "OWNER_SID_LOGON_SESSION_TOKEN_NOT_LINKED",
        "CONNECTED_PIPE_HANDLE_NOT_BOUND_TO_SECURITY_CONTEXT",
        "SSPI_SIGNATURE_VERIFICATION_SEQUENCE_NOT_IMPLEMENTED",
        "PUBLISHER_RELEASE_GENERATION_AND_PROTECTED_INSTALL_NOT_PROVEN",
        "CHALLENGE_NONCE_RNG_AND_DURABLE_REPLAY_FENCE_NOT_PROVEN",
    ), init=False)


class OfflineSspiHandshakeCandidate:
    """Single-thread, one-epoch, non-rearmable, strict role-alternating mock.

    OBSERVED final flags are checked ONLY after both sides return OK.
    A caller-forged record might pass the mock contract; it still can never
    create authorization, and cannot be passed to product/Broker/ledger.
    """
    def __init__(self, pins, *, _clock_ms=None, timeout_ms=1000):
        if type(pins) is not SspiCandidatePins:
            raise SspiCandidateDenied("EXACT_PIN_CONTRACT_REQUIRED")
        pins.__post_init__()
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
            raise SspiCandidateDenied("INVALID_TIMEOUT")
        self._pins = pins
        self._clock = _clock_ms if _clock_ms is not None else lambda: monotonic_ns() // 1_000_000
        self._timeout_ms = timeout_ms
        self._owner_thread = get_ident()
        self._last_tick = -1
        self._start_tick = None
        self._last_ordinal = -1
        self._final_roles = set()
        self._used_token_sha = set()
        self._digest = sha256(DOMAIN)
        # Bind the mock transcript to exact generation / epoch / both nonces.
        # This is an *unkeyed* audit digest, not an SSPI channel MAC.
        pin_bytes = json.dumps(asdict(pins), sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True).encode("ascii")
        self._digest.update(len(pin_bytes).to_bytes(4, "little"))
        self._digest.update(pin_bytes)
        self._halted = False
        self._finished = False

    def _tick(self):
        value = self._clock()
        if (type(value) is not int or not 0 <= value < 1 << 63
                or value < self._last_tick):
            raise SspiCandidateDenied("CLOCK_ROLLBACK_OR_INVALID")
        if self._start_tick is None:
            self._start_tick = value
        if value - self._start_tick >= self._timeout_ms:
            raise SspiCandidateDenied("SSPI_HANDSHAKE_TIMEOUT")
        self._last_tick = value

    def observe_step(self, step):
        if self._halted or self._finished:
            raise SspiCandidateDenied("HANDSHAKE_TERMINAL")
        try:
            if get_ident() != self._owner_thread:
                raise SspiCandidateDenied("CROSS_THREAD_HANDSHAKE")
            if type(step) is not SspiStepCandidate:
                raise SspiCandidateDenied("EXACT_STEP_REQUIRED")
            self._tick()
            length = self._last_ordinal + 1
            if length >= MAX_STEPS or type(step.ordinal) is not int or step.ordinal != length:
                raise SspiCandidateDenied("HANDSHAKE_SEQUENCE_INVALID")
            expected = "CLIENT" if length % 2 == 0 else "SERVER"
            if step.role != expected or step.role in self._final_roles:
                raise SspiCandidateDenied("HANDSHAKE_ROLE_OR_FINAL_REPLAY")
            if step.outcome not in ("CONTINUE", "OK"):
                raise SspiCandidateDenied("SSPI_INCOMPLETE_OR_FAILED")
            if (type(step.output_token) is not bytes or
                    len(step.output_token) > MAX_TOKEN_BYTES or
                    (step.outcome == "CONTINUE" and len(step.output_token) == 0)):
                raise SspiCandidateDenied("OPAQUE_TOKEN_BOUNDS")
            if (type(step.returned_attributes) is not frozenset
                    or len(step.returned_attributes) > len(ALLOWED_ATTRIBUTES)
                    or any(type(x) is not str or x not in ALLOWED_ATTRIBUTES
                           for x in step.returned_attributes)):
                raise SspiCandidateDenied("INVALID_SSPI_ATTRIBUTE_SET")
            if (step.installation_id != self._pins.installation_id or
                    step.generation != self._pins.generation or
                    step.broker_instance_id != self._pins.broker_instance_id or
                    type(step.connection_epoch) is not int or
                    step.connection_epoch != self._pins.connection_epoch or
                    step.client_nonce != self._pins.client_nonce or
                    step.server_nonce != self._pins.server_nonce):
                raise SspiCandidateDenied("HANDSHAKE_PIN_OR_EPOCH_MISMATCH")
            # Intermediate attributes are never evidence of final auth.
            if step.outcome == "CONTINUE":
                if step.selected_package or step.observed_peer_principal:
                    raise SspiCandidateDenied("PREMATURE_PEER_IDENTITY_CLAIM")
            else:
                if step.selected_package != REVIEWED_NEGOTIATED_PACKAGE:
                    raise SspiCandidateDenied("SSPI_PACKAGE_DOWNGRADE_OR_UNKNOWN")
                if not REQUIRED_FINAL_ATTRIBUTES.issubset(step.returned_attributes):
                    raise SspiCandidateDenied("MISSING_FINAL_CONTEXT_ATTRIBUTES")
                expected_principal = (
                    self._pins.service_principal if step.role == "CLIENT"
                    else self._pins.owner_principal)
                if step.observed_peer_principal != expected_principal:
                    raise SspiCandidateDenied("PEER_PRINCIPAL_MISMATCH")
            token_id = sha256(step.output_token).digest()
            if step.output_token and token_id in self._used_token_sha:
                raise SspiCandidateDenied("DUPLICATE_HANDSHAKE_TOKEN")
            if step.output_token:
                self._used_token_sha.add(token_id)
            self._digest.update(bytes((len(step.role),)))
            self._digest.update(step.role.encode("ascii"))
            self._digest.update(step.ordinal.to_bytes(2, "little"))
            self._digest.update(bytes((len(step.outcome),)))
            self._digest.update(step.outcome.encode("ascii"))
            self._digest.update(len(step.output_token).to_bytes(4, "little"))
            self._digest.update(step.output_token)
            # Do not retain opaque SSPI token bytes in the session object.
            self._last_ordinal = step.ordinal
            if step.outcome == "OK":
                self._final_roles.add(step.role)
            self._tick()
        except BaseException:
            self._halted = True
            raise

    def close_data_only(self):
        """Terminal mock review; always denies native service/ledger admission."""
        if self._halted or self._finished:
            raise SspiCandidateDenied("HANDSHAKE_TERMINAL")
        try:
            if get_ident() != self._owner_thread:
                raise SspiCandidateDenied("CROSS_THREAD_HANDSHAKE")
            self._tick()
            if self._final_roles != {"CLIENT", "SERVER"}:
                raise SspiCandidateDenied("BOTH_FINAL_CONTEXTS_REQUIRED")
            self._finished = True
            return SspiDataOnlyReview(self._digest.hexdigest(), self._last_ordinal + 1,
                                      True, True, REVIEWED_NEGOTIATED_PACKAGE)
        except BaseException:
            self._halted = True
            raise
