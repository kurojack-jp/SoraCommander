"""Phase 9I: offline Ed25519 receipt verification with an EXTERNAL trust key.

No private keys in the application, no signer, installer, state mutation,
network, Windows ACLs, elevation, Task or Remote MCP. Even VALID signatures
do not authorize deployment; the caller-supplied trusted public key and
version floor MUST originate in a separately protected trust channel.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import re

from .release_inventory import CandidateIntegrityError
from .release_layout import ReleaseLayout, ReleaseLayoutError
from .release_preflight import inspect_offline_candidate

RECEIPT_FORMAT = "sora-commander-next-ed25519-receipt-v1"
PRODUCT = "sora-commander-next"
DOMAIN = b"SoraCommanderNext Release Receipt v1\x00"
MAX_RECEIPT_BYTES = 4096
MAX_SEQUENCE = (1 << 63) - 1
SHA_PATTERN = re.compile(r"[0-9a-f]{64}")
PUBLISHER_PATTERN = re.compile(r"[a-z0-9_][a-z0-9_-]{2,63}")


class ReleaseReceiptError(ValueError):
    """Not a valid release from the independently trusted publisher."""


@dataclass(frozen=True)
class VerifiedRelease:
    publisher: str
    sequence: int
    manifest_sha256: str
    verified_public_key_fingerprint: str


@dataclass(frozen=True)
class SignedOfflinePreview:
    receipt: VerifiedRelease
    signature_verified_against_supplied_key: bool
    candidate_verified_against_signed_digest: bool
    status: str = "SIGNED_OFFLINE_PREVIEW_DEPLOYMENT_HOLD"
    deployment_authorized: bool = False
    remote_authorized: bool = False
    launch_permitted: bool = False
    trust_boundary: str = (
        "Public key and sequence floor are caller supplied; their provenance "
        "and protected persistence are not authenticated by this module."
    )


def _unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ReleaseReceiptError("duplicate release receipt field")
        out[key] = value
    return out


def _canonical(obj):
    return (json.dumps(obj, sort_keys=True, ensure_ascii=True,
                       separators=(",", ":")) + "\n").encode("ascii")


def _validate_payload(payload):
    if (type(payload) is not dict or
            set(payload) != {"format", "product", "publisher", "sequence", "manifest_sha256"} or
            payload["format"] != RECEIPT_FORMAT or payload["product"] != PRODUCT or
            type(payload["publisher"]) is not str or
            PUBLISHER_PATTERN.fullmatch(payload["publisher"]) is None or
            type(payload["sequence"]) is not int or
            not 1 <= payload["sequence"] <= MAX_SEQUENCE or
            type(payload["manifest_sha256"]) is not str or
            SHA_PATTERN.fullmatch(payload["manifest_sha256"]) is None):
        raise ReleaseReceiptError("invalid signed release payload")
    return payload


def receipt_signing_bytes(payload):
    """Stable, domain-separated bytes a separately trusted publisher signs."""
    _validate_payload(payload)
    return DOMAIN + _canonical(payload)


def verify_signed_receipt(receipt_bytes, *,
                          trusted_ed25519_public_key,
                          trusted_publisher,
                          minimum_sequence,
                          installed_sequence=None):
    """Verify a detached Ed25519 signature under an EXTERNALLY PINNED key.

    minimum_sequence and installed_sequence must be read from a trusted,
    non-rollbackable source by the *caller*. No persistent state is modified.
    With installed_sequence, this is a proposed UPGRADE and must be newer.
    """
    if (type(trusted_ed25519_public_key) is not bytes or
            len(trusted_ed25519_public_key) != 32 or
            type(trusted_publisher) is not str or
            PUBLISHER_PATTERN.fullmatch(trusted_publisher) is None or
            type(minimum_sequence) is not int or
            not 1 <= minimum_sequence <= MAX_SEQUENCE or
            (installed_sequence is not None and
             (type(installed_sequence) is not int or
              not 0 <= installed_sequence <= MAX_SEQUENCE))):
        raise ReleaseReceiptError("untrusted or invalid caller release policy")
    if (type(receipt_bytes) is not bytes or
            not 0 < len(receipt_bytes) <= MAX_RECEIPT_BYTES):
        raise ReleaseReceiptError("receipt must be bounded bytes")
    try:
        receipt = json.loads(receipt_bytes.decode("ascii"),
                             object_pairs_hook=_unique_pairs)
    except (UnicodeError, ValueError, TypeError, OverflowError) as exc:
        raise ReleaseReceiptError("malformed release receipt") from exc
    if (type(receipt) is not dict or
            set(receipt) != {"format", "payload", "signature_hex"} or
            receipt["format"] != RECEIPT_FORMAT or
            type(receipt["signature_hex"]) is not str or
            re.fullmatch(r"[0-9a-f]{128}", receipt["signature_hex"]) is None):
        raise ReleaseReceiptError("invalid release receipt envelope")
    payload = _validate_payload(receipt["payload"])
    if receipt_bytes != _canonical(receipt):
        raise ReleaseReceiptError("noncanonical release receipt")
    if payload["publisher"] != trusted_publisher:
        raise ReleaseReceiptError("publisher identity does not match trust policy")
    if payload["sequence"] < minimum_sequence:
        raise ReleaseReceiptError("release is below trusted minimum sequence")
    if installed_sequence is not None and payload["sequence"] <= installed_sequence:
        raise ReleaseReceiptError("release downgrade or installed-version replay")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        public_key = Ed25519PublicKey.from_public_bytes(trusted_ed25519_public_key)
        public_key.verify(bytes.fromhex(receipt["signature_hex"]),
                          receipt_signing_bytes(payload))
    except ImportError as exc:
        raise ReleaseReceiptError("Ed25519 verifier dependency unavailable") from exc
    except (ValueError, InvalidSignature) as exc:
        raise ReleaseReceiptError("publisher Ed25519 signature invalid") from exc
    return VerifiedRelease(
        publisher=trusted_publisher,
        sequence=payload["sequence"],
        manifest_sha256=payload["manifest_sha256"],
        verified_public_key_fingerprint=sha256(trusted_ed25519_public_key).hexdigest(),
    )


def preview_signed_candidate(candidate_root, *,
                             receipt_bytes,
                             trusted_ed25519_public_key,
                             trusted_publisher,
                             minimum_sequence,
                             layout,
                             installed_sequence=None):
    """Both cryptographic receipt and candidate must verify; still HOLD."""
    if type(layout) is not ReleaseLayout:
        raise ReleaseLayoutError("reviewed Windows paths required")
    verified = verify_signed_receipt(
        receipt_bytes, trusted_ed25519_public_key=trusted_ed25519_public_key,
        trusted_publisher=trusted_publisher,
        minimum_sequence=minimum_sequence,
        installed_sequence=installed_sequence)
    stage = inspect_offline_candidate(
        candidate_root, expected_manifest_sha256=verified.manifest_sha256,
        layout=layout)
    if not stage.content_verified or stage.launch_permitted:
        raise CandidateIntegrityError("unexpected offline preflight result")
    return SignedOfflinePreview(
        receipt=verified,
        signature_verified_against_supplied_key=True,
        candidate_verified_against_signed_digest=True,
    )
