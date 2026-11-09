"""Phase 9J: safe, signed-update PREVIEW. No installation or execution.

Release owner must supply trusted signing key and protected installed
sequence out-of-band. No code, ACL, Task, network, credentials or persistent
state is changed. No rollback operation is executed by this module.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from .plugin_icon import inspect_icon_png, MAX_ICON_BYTES
from .release_inventory import ASSETS, PLUGIN_ICON, CandidateIntegrityError
from .release_receipt import preview_signed_candidate


class UpdatePolicyError(ValueError):
    pass


@dataclass(frozen=True)
class OfflineUpdatePreview:
    status: str
    from_sequence: int
    to_sequence: int
    publisher: str
    public_key_fingerprint: str
    new_icon_sha256: str | None
    retain_previous_slot: bool = True
    requires_owner_release_review: bool = True
    installation_permitted: bool = False
    remote_permitted: bool = False
    activation_permitted: bool = False


def _digest_or_none(value):
    return value is None or (
        type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def inspect_signed_update(candidate_root, *, receipt_bytes,
                          trusted_ed25519_public_key, trusted_publisher,
                          trusted_minimum_sequence, protected_installed_sequence,
                          layout, current_icon_sha256=None,
                          approved_new_icon_sha256=None):
    """Strict monotonic upgrade and icon-preservation rules, with HOLD result.

    Installed sequence, current image digest, and trusted publisher policy
    MUST originate from protected storage; this function does not provide it.
    """
    if (type(protected_installed_sequence) is not int or
            protected_installed_sequence < 0 or
            not _digest_or_none(current_icon_sha256) or
            not _digest_or_none(approved_new_icon_sha256)):
        raise UpdatePolicyError("trusted installed sequence and image policy required")

    verified = preview_signed_candidate(
        candidate_root, receipt_bytes=receipt_bytes,
        trusted_ed25519_public_key=trusted_ed25519_public_key,
        trusted_publisher=trusted_publisher,
        minimum_sequence=trusted_minimum_sequence, layout=layout,
        installed_sequence=protected_installed_sequence)
    if (not verified.signature_verified_against_supplied_key or
            not verified.candidate_verified_against_signed_digest or
            verified.launch_permitted):
        raise UpdatePolicyError("unverified candidate cannot be updated")

    path = Path(candidate_root) / ASSETS / PLUGIN_ICON
    new_icon = None
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ICON_BYTES:
            raise UpdatePolicyError("invalid update icon path")
        data = path.read_bytes()
        inspect_icon_png(data)
        new_icon = sha256(data).hexdigest()

    # An installed identity is never silently lost or changed.
    if current_icon_sha256 is not None:
        if new_icon is None:
            raise UpdatePolicyError("update removed previously approved branding")
        if new_icon != current_icon_sha256 and new_icon != approved_new_icon_sha256:
            raise UpdatePolicyError("branding changed without explicit Owner approval")
    elif new_icon is not None and new_icon != approved_new_icon_sha256:
        raise UpdatePolicyError("new branding requires explicit Owner approval")

    # Owner may not approve a different image than the signed candidate.
    if approved_new_icon_sha256 is not None and approved_new_icon_sha256 != new_icon:
        raise UpdatePolicyError("approved image digest does not match candidate")

    return OfflineUpdatePreview(
        status="SIGNED_UPDATE_VALIDATED_INSTALL_AND_ACTIVATION_HOLD",
        from_sequence=protected_installed_sequence,
        to_sequence=verified.receipt.sequence,
        publisher=verified.receipt.publisher,
        public_key_fingerprint=verified.receipt.verified_public_key_fingerprint,
        new_icon_sha256=new_icon,
    )
