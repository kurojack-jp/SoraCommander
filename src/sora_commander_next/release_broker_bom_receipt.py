"""Offline Broker complete BOM v2 verification, under an externally pinned key.

Only signature evidence: no files are inspected, installed or executed. The
caller must independently authenticate/protect the public key, publisher and
sequence policy. A valid signature does not prove that trust anchor's origin
or the provenance of code. No private keys or signing operations live here.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re

RECEIPT_FORMAT = "sora-commander-next-broker-bom-v2"
PRODUCT = "sora-commander-next"
DOMAIN = b"SoraCommanderNext Broker Complete BOM v2\x00"
MAX_RECEIPT_BYTES = 32 * 1024
MAX_SEQUENCE = (1 << 63) - 1
MAX_ARTIFACT_BYTES = (1 << 63) - 1
MAX_ENTRIES = 64
SHA_PATTERN = re.compile(r"[0-9a-f]{64}")
PUBLISHER_PATTERN = re.compile(r"[a-z0-9_][a-z0-9_-]{2,63}")
# distribution-version(-build)-python-abi-platform.whl; no directory syntax.
WHEEL_PATTERN = re.compile(
    r"[A-Za-z0-9_]+-[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*"
    r"(?:-[0-9][A-Za-z0-9_]*)?"
    r"-[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*"
    r"-[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*"
    r"-[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*\.whl"
)
PATH_COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
RESERVED_NAMES = frozenset((
    "con", "prn", "aux", "nul", "clock$", "conin$", "conout$",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
))


class BrokerBomReceiptError(ValueError):
    """Invalid v2 receipt or external trust policy; never an authorization."""


@dataclass(frozen=True)
class WheelPin:
    filename: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class NativeBinaryPin:
    role: str
    relative_path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class VerifiedBrokerBom:
    publisher: str
    sequence: int
    candidate_manifest_sha256: str
    target_python: str
    target_platform: str
    wheels: tuple[WheelPin, ...]
    native_binaries: tuple[NativeBinaryPin, ...]
    verified_public_key_fingerprint: str
    status: str = field(default="HOLD_LIVE", init=False)
    deployment_authorized: bool = field(default=False, init=False)
    remote_authorized: bool = field(default=False, init=False)
    code_provenance_authenticated: bool = field(default=False, init=False)
    external_trust_anchor_origin_verified: bool = field(default=False, init=False)
    install_permitted: bool = field(default=False, init=False)


def _canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("ascii")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BrokerBomReceiptError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value):
    raise BrokerBomReceiptError("nonfinite JSON number")


def _valid_digest(value):
    return type(value) is str and SHA_PATTERN.fullmatch(value) is not None


def _valid_publisher(value):
    return type(value) is str and PUBLISHER_PATTERN.fullmatch(value) is not None


def _valid_native_path(value):
    if type(value) is not str or not 1 <= len(value) <= 240:
        return False
    for part in value.split("/"):
        if (not 1 <= len(part) <= 255 or
                PATH_COMPONENT.fullmatch(part) is None or
                part.endswith(".") or
                part.split(".", 1)[0].casefold() in RESERVED_NAMES):
            return False
    return True


def _validate_pins(records, *, native):
    if type(records) is not list or not 1 <= len(records) <= MAX_ENTRIES:
        raise BrokerBomReceiptError("bounded nonempty BOM list required")
    keys = ({"role", "relative_path", "sha256", "bytes"} if native else
            {"filename", "sha256", "bytes"})
    names = []
    interpreters = 0
    for item in records:
        if (type(item) is not dict or set(item) != keys or
                not _valid_digest(item["sha256"]) or
                type(item["bytes"]) is not int or
                not 1 <= item["bytes"] <= MAX_ARTIFACT_BYTES):
            raise BrokerBomReceiptError("invalid BOM pin")
        if native:
            name = item["relative_path"]
            if not _valid_native_path(name) or type(item["role"]) is not str:
                raise BrokerBomReceiptError("invalid native binary path or role")
            suffix = name.rsplit(".", 1)[-1].casefold()
            if item["role"] == "interpreter" and suffix == "exe":
                interpreters += 1
            elif item["role"] != "dependency" or suffix not in ("exe", "dll", "pyd"):
                raise BrokerBomReceiptError("unsupported native binary role or suffix")
        else:
            name = item["filename"]
            if (type(name) is not str or not 1 <= len(name) <= 255 or
                    WHEEL_PATTERN.fullmatch(name) is None):
                raise BrokerBomReceiptError("invalid wheel filename")
        names.append(name.casefold())
    if names != sorted(names) or len(set(names)) != len(names):
        raise BrokerBomReceiptError("BOM pins must be casefold sorted and unique")
    if native and interpreters != 1:
        raise BrokerBomReceiptError("exactly one interpreter executable required")


def _validate_payload(payload):
    if (type(payload) is not dict or set(payload) != {
            "format", "product", "publisher", "sequence",
            "candidate_manifest_sha256", "target_python", "target_platform",
            "wheels", "native_binaries"} or
            payload["format"] != RECEIPT_FORMAT or payload["product"] != PRODUCT or
            not _valid_publisher(payload["publisher"]) or
            type(payload["sequence"]) is not int or
            not 1 <= payload["sequence"] <= MAX_SEQUENCE or
            not _valid_digest(payload["candidate_manifest_sha256"]) or
            payload["target_python"] != "cp313" or
            payload["target_platform"] != "win_amd64"):
        raise BrokerBomReceiptError("invalid v2 Broker BOM payload or target")
    _validate_pins(payload["wheels"], native=False)
    _validate_pins(payload["native_binaries"], native=True)


def receipt_signing_bytes(payload):
    """Data only: exact DOMAIN + canonical payload + LF for an external signer.

    Envelope is exactly {format, payload, signature_hex}, canonical JSON with
    sorted keys, ASCII encoding, compact separators and a single terminal LF.
    Signature is lowercase hex for 64 Ed25519 bytes; it is never a trust key.
    """
    _validate_payload(payload)
    canonical = _canonical(payload)
    # Account for the entire fixed envelope, including the 128 hex characters.
    envelope_size = len(_canonical({"format": RECEIPT_FORMAT, "payload": payload,
                                    "signature_hex": "0" * 128}))
    if envelope_size > MAX_RECEIPT_BYTES:
        raise BrokerBomReceiptError("BOM exceeds receipt byte limit")
    return DOMAIN + canonical


def assemble_externally_signed_bom(payload, signature_bytes=None):
    """Assemble external signer output as canonical DATA_ONLY envelope bytes.

    Owner first independently verifies and protects the public key fingerprint.
    Operator prepares exact receipt_signing_bytes(payload) for offline external
    Ed25519 signing: sign those bytes directly, including DOMAIN. Signing just
    canonical payload JSON (without DOMAIN), a digest instead of these bytes,
    or the envelope is invalid. Receive the raw 64-byte signature and call this
    assembler. Then validate via release_trust_policy.review_pinned_signed_bom
    with the external public key, separately obtained policy and expected
    candidate manifest digest. The final result remains DATA_ONLY and G1 HOLD.

    Assembly never claims signature validity: only verify_signed_receipt with
    a trusted external public key proves mathematical validity. No private key
    is accepted, generated or persisted; no files are written, installation or
    elevation performed, or runtime authority granted. Inputs are not mutated
    or normalized. Missing signatures raise BrokerBomReceiptError as well.
    """
    if type(signature_bytes) is not bytes or len(signature_bytes) != 64:
        raise BrokerBomReceiptError("external signature must be exactly 64 raw bytes")
    receipt_signing_bytes(payload)
    envelope = _canonical({"format": RECEIPT_FORMAT, "payload": payload,
                           "signature_hex": signature_bytes.hex()})
    if len(envelope) > MAX_RECEIPT_BYTES:
        raise BrokerBomReceiptError("BOM exceeds receipt byte limit")
    return envelope


def verify_signed_receipt(receipt_bytes, *, trusted_ed25519_public_key,
                          trusted_publisher, minimum_sequence,
                          installed_sequence=None):
    """Verify under supplied immutable trust inputs; never inspect artifacts.

    minimum_sequence is inclusive. installed_sequence, when supplied, demands
    a strictly newer release. Caller owns protected, nonrollbackable storage.
    """
    if (type(trusted_ed25519_public_key) is not bytes or
            len(trusted_ed25519_public_key) != 32 or
            not _valid_publisher(trusted_publisher) or
            type(minimum_sequence) is not int or
            not 1 <= minimum_sequence <= MAX_SEQUENCE or
            (installed_sequence is not None and
             (type(installed_sequence) is not int or
              not 0 <= installed_sequence <= MAX_SEQUENCE))):
        raise BrokerBomReceiptError("invalid external trust policy")
    if (type(receipt_bytes) is not bytes or
            not 0 < len(receipt_bytes) <= MAX_RECEIPT_BYTES):
        raise BrokerBomReceiptError("receipt must be bounded immutable bytes")
    try:
        receipt = json.loads(receipt_bytes.decode("ascii"),
                             object_pairs_hook=_unique_pairs,
                             parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BrokerBomReceiptError("malformed v2 receipt JSON") from exc
    if (type(receipt) is not dict or
            set(receipt) != {"format", "payload", "signature_hex"} or
            receipt["format"] != RECEIPT_FORMAT or
            type(receipt["signature_hex"]) is not str or
            re.fullmatch(r"[0-9a-f]{128}", receipt["signature_hex"]) is None):
        raise BrokerBomReceiptError("invalid v2 receipt envelope")
    payload = receipt["payload"]
    signing_bytes = receipt_signing_bytes(payload)
    if receipt_bytes != _canonical(receipt):
        raise BrokerBomReceiptError("noncanonical v2 receipt")
    if payload["publisher"] != trusted_publisher:
        raise BrokerBomReceiptError("publisher differs from external trust policy")
    if payload["sequence"] < minimum_sequence:
        raise BrokerBomReceiptError("release below trusted minimum sequence")
    if installed_sequence is not None and payload["sequence"] <= installed_sequence:
        raise BrokerBomReceiptError("installed sequence replay or rollback")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise BrokerBomReceiptError("Ed25519 dependency unavailable") from exc
    try:
        key = Ed25519PublicKey.from_public_bytes(trusted_ed25519_public_key)
        key.verify(bytes.fromhex(receipt["signature_hex"]), signing_bytes)
    except (ValueError, InvalidSignature) as exc:
        raise BrokerBomReceiptError("invalid Broker BOM Ed25519 signature") from exc
    return VerifiedBrokerBom(
        publisher=payload["publisher"], sequence=payload["sequence"],
        candidate_manifest_sha256=payload["candidate_manifest_sha256"],
        target_python=payload["target_python"], target_platform=payload["target_platform"],
        wheels=tuple(WheelPin(**item) for item in payload["wheels"]),
        native_binaries=tuple(NativeBinaryPin(**item) for item in payload["native_binaries"]),
        verified_public_key_fingerprint=sha256(trusted_ed25519_public_key).hexdigest(),
    )
