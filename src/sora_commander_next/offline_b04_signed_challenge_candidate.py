"""DEV_ONLY B04 P-256 signed challenge verifier, proof of math NOT authority.

Supports an offline future standalone-Windows channel design. Test keys are
generated in RAM by tests only; this module never signs, imports a private
key, opens CNG/DPAPI/Win32, provisions a service, creates a pipe or IPC, or
enables a job. Public key/pin/expected context supplied by this call are
UNTRUSTED until an independently protected installer/release anchor proves
their origin. The default/no-anchor operation FAILS CLOSED.

This verifier accepts strict DER ECDSA-SHA256, not the native CNG
BCryptSignHash IEEE-P1363 (r||s) signature format. Any conversion at the
native boundary requires separate independent review and OS acceptance.
"""
from dataclasses import dataclass, field, asdict
from hashlib import sha256
from threading import get_ident
from time import monotonic_ns
import json
import re

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature, encode_dss_signature,
)

DOMAIN = b"sora-commander-next/b04-protected-service-ecdsa-challenge/v1\x00"
FORMAT = "scn-b04-signed-service-challenge-v1"
MAX_PAYLOAD = 8192
MAX_SIGNATURE = 128
MAX_PUBLIC_DER = 256
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
SID = re.compile(r"S-1-(?:0|[1-9][0-9]*)(?:-(?:0|[1-9][0-9]*)){1,15}\Z")
SERVICE_SID = re.compile(r"S-1-5-80(?:-(?:0|[1-9][0-9]*)){5}\Z")
# SECP256R1 (P-256) group order, specified by SEC2/NIST; low-S canonical form.
P256_ORDER = int(
    "ffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551", 16
)


class SignedChallengeDenied(ValueError):
    def __init__(self, code):
        super().__init__(code + "; NOT_AUTHENTICATED / UNAVAILABLE")
        self.code = code


def _hex(value, pattern):
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise SignedChallengeDenied("INVALID_PINNED_HASH_OR_NONCE")


def _sid(value):
    if type(value) is not str or len(value) > 184 or SID.fullmatch(value) is None:
        raise SignedChallengeDenied("INVALID_SID_FORMAT")


def _number(value, maximum):
    if type(value) is not int or not 1 <= value < maximum:
        raise SignedChallengeDenied("INVALID_INTEGER_BINDING")


@dataclass(frozen=True, slots=True)
class SignedChallengeExpectation:
    installation_id: str
    generation: str
    broker_instance_id: str
    client_nonce: str
    service_nonce: str
    connection_epoch: int
    owner_sid: str
    owner_logon_luid: tuple
    owner_session_id: int
    service_sid: str
    installed_release_sha256: str
    approved_service_image_sha256: str
    command_snapshot_sha256: str
    deadline_tick_ms: int

    def __post_init__(self):
        for name in ("installation_id", "generation", "broker_instance_id"):
            _hex(getattr(self,name), HEX32)
        for name in ("client_nonce", "service_nonce", "installed_release_sha256",
                     "approved_service_image_sha256", "command_snapshot_sha256"):
            _hex(getattr(self,name), HEX64)
        if self.client_nonce == self.service_nonce:
            raise SignedChallengeDenied("NONCE_COLLISION")
        _number(self.connection_epoch, 1 << 63)
        if (type(self.owner_logon_luid) is not tuple
                or len(self.owner_logon_luid) != 2
                or any(type(v) is not int or not 0 <= v < 1 << 32
                       for v in self.owner_logon_luid)):
            raise SignedChallengeDenied("INVALID_OWNER_LOGON_LUID")
        _number(self.owner_session_id, 1 << 32)
        _number(self.deadline_tick_ms, 1 << 63)
        _sid(self.owner_sid)
        _sid(self.service_sid)
        if SERVICE_SID.fullmatch(self.service_sid) is None:
            raise SignedChallengeDenied("INVALID_SERVICE_SID_FORM")
        if self.owner_sid == self.service_sid:
            raise SignedChallengeDenied("OWNER_AND_SERVICE_SID_COLLISION")


@dataclass(frozen=True, slots=True)
class MathematicalSignatureObservation:
    payload_sha256: str
    signature_sha256: str
    public_spki_sha256: str
    connection_epoch: int
    verification_algorithm: str = field(default="ECDSA_P256_SHA256_DER_LOW_S", init=False)
    mathematical_signature_verified: bool = field(default=True, init=False)
    independent_installer_key_anchor_verified: bool = field(default=False, init=False)
    service_private_key_nonexportable_os_verified: bool = field(default=False, init=False)
    private_key_acl_and_service_sid_verified: bool = field(default=False, init=False)
    connected_named_pipe_transcript_bound: bool = field(default=False, init=False)
    sign_operation_was_by_live_service_proven: bool = field(default=False, init=False)
    challenge_nonce_rng_os_verified: bool = field(default=False, init=False)
    publisher_and_release_generation_os_verified: bool = field(default=False, init=False)
    native_cng_signature_abi_verified: bool = field(default=False, init=False)
    authentication: str = field(default="NOT_AUTHENTICATED", init=False)
    admission: str = field(default="UNAVAILABLE", init=False)
    missing_evidence: tuple = field(default=(
        "INDEPENDENT_INSTALLER_PUBLIC_KEY_ANCHOR_NOT_ACCEPTED",
        "CNG_NCRYPT_NONEXPORTABLE_SERVICE_KEY_PROTECTION_NOT_ACCEPTED",
        "PRIVATE_KEY_PROVENANCE_AND_SERVICE_SID_ACL_NOT_ACCEPTED",
        "PIPE_CONNECTED_HANDLE_AND_SIGNED_TRANSCRIPT_NOT_OS_BOUND",
        "LIVE_SERVICE_PROCESS_HANDLE_AND_PUBLISHER_NOT_OS_BOUND",
        "NONCE_ENTROPY_AND_DURABLE_CHALLENGE_REPLAY_FENCE_NOT_OS_VERIFIED",
        "CNG_P1363_TO_DER_CONVERSION_NOT_NATIVE_REVIEWED",
    ), init=False)


def canonical_payload_bytes(expectation):
    """Produce exact *expected* data. No keys, no signing and no authority."""
    if type(expectation) is not SignedChallengeExpectation:
        raise SignedChallengeDenied("EXACT_CHALLENGE_EXPECTATION_REQUIRED")
    expectation.__post_init__()
    obj = {"format": FORMAT, **asdict(expectation)}
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=True,
                     allow_nan=False, separators=(",", ":")).encode("ascii")
    if not 0 < len(raw) <= MAX_PAYLOAD:
        raise SignedChallengeDenied("SIGNED_CHALLENGE_OVERSIZE")
    return raw


def _pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise SignedChallengeDenied("DUPLICATE_CANONICAL_FIELD")
        obj[key] = value
    return obj


def _verified_payload(raw, expected):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_PAYLOAD:
        raise SignedChallengeDenied("INVALID_CHALLENGE_FRAME")
    try:
        parsed = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs)
    except SignedChallengeDenied:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise SignedChallengeDenied("INVALID_CANONICAL_CHALLENGE") from exc
    reference = canonical_payload_bytes(expected)
    if type(parsed) is not dict or set(parsed) != {
        "format", *asdict(expected).keys()
    } or raw != reference:
        raise SignedChallengeDenied("CHALLENGE_CONTEXT_OR_CANONICAL_MISMATCH")


def verify_signed_service_challenge_data_only(
    *, expected, payload, signature_der, public_spki_der,
    independently_pinned_spki_sha256, _clock_ms=None,
):
    """Cryptographically verify an injected public key and signed payload.

    Even valid ECDSA cannot prove that the public key was protected/anchored
    by an independent installer. This function does not return capabilities
    or grant permission to Broker, service, protected ledger or real job.
    """
    if type(expected) is not SignedChallengeExpectation:
        raise SignedChallengeDenied("EXACT_CHALLENGE_EXPECTATION_REQUIRED")
    expected.__post_init__()
    _hex(independently_pinned_spki_sha256, HEX64)
    if (type(public_spki_der) is not bytes or
            not 1 <= len(public_spki_der) <= MAX_PUBLIC_DER):
        raise SignedChallengeDenied("INVALID_PUBLIC_KEY_BYTES")
    if sha256(public_spki_der).hexdigest() != independently_pinned_spki_sha256:
        raise SignedChallengeDenied("PUBLIC_KEY_NOT_PINNED")
    if (type(signature_der) is not bytes or
            not 1 <= len(signature_der) <= MAX_SIGNATURE):
        raise SignedChallengeDenied("INVALID_SIGNATURE_SIZE")
    _verified_payload(payload, expected)
    clock = _clock_ms if _clock_ms is not None else lambda: monotonic_ns()//1_000_000
    now = clock()
    if type(now) is not int or not 0 <= now < expected.deadline_tick_ms:
        raise SignedChallengeDenied("EXPIRED_OR_INVALID_CHALLENGE_CLOCK")
    try:
        public = serialization.load_der_public_key(public_spki_der)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise SignedChallengeDenied("INVALID_DER_PUBLIC_KEY") from exc
    if not isinstance(public, ec.EllipticCurvePublicKey) or (
            not isinstance(public.curve, ec.SECP256R1)):
        raise SignedChallengeDenied("UNACCEPTED_PUBLIC_KEY_ALGORITHM")
    if public.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo) != public_spki_der:
        raise SignedChallengeDenied("NONCANONICAL_PUBLIC_KEY")
    try:
        r, s = decode_dss_signature(signature_der)
        if (not 1 <= r < P256_ORDER or
                not 1 <= s <= P256_ORDER//2 or
                encode_dss_signature(r,s) != signature_der):
            raise SignedChallengeDenied("NONCANONICAL_SIGNATURE")
    except SignedChallengeDenied:
        raise
    except ValueError as exc:
        raise SignedChallengeDenied("INVALID_DER_SIGNATURE") from exc
    try:
        public.verify(signature_der, DOMAIN+payload, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise SignedChallengeDenied("ECDSA_SIGNATURE_INVALID") from exc
    return MathematicalSignatureObservation(
        sha256(payload).hexdigest(), sha256(signature_der).hexdigest(),
        sha256(public_spki_der).hexdigest(), expected.connection_epoch
    )
