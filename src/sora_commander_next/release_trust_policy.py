"""Phase9N offline release trust-policy receipt and anti-rollback comparison.

Pure data validation. The *caller* must supply a trustworthy externally
pinned publisher key fingerprint and monotonically protected sequence.
No mutable JSON file is treated as authoritative by this module.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re

from . import release_broker_bom_receipt

SHA = re.compile(r"[0-9a-f]{64}")
PUB = re.compile(r"[a-z0-9_][a-z0-9_-]{2,63}")
MAX_SEQUENCE = (1 << 63) - 1

class TrustPolicyError(ValueError):
    pass

@dataclass(frozen=True)
class TrustedReleasePolicy:
    publisher: str
    public_key_sha256: str
    minimum_sequence: int
    installed_sequence: int

def review_policy(*, publisher, public_key_sha256,
                  minimum_sequence, installed_sequence):
    if (type(publisher) is not str or PUB.fullmatch(publisher) is None
            or type(public_key_sha256) is not str
            or SHA.fullmatch(public_key_sha256) is None
            or type(minimum_sequence) is not int
            or type(installed_sequence) is not int
            or not 1 <= minimum_sequence <= MAX_SEQUENCE
            or not 0 <= installed_sequence <= MAX_SEQUENCE):
        raise TrustPolicyError("invalid external trust policy")
    return TrustedReleasePolicy(publisher, public_key_sha256,
                                minimum_sequence, installed_sequence)

def evaluate_upgrade(*, trusted_policy, received_public_key_sha256,
                     signed_publisher, proposed_sequence):
    if type(trusted_policy) is not TrustedReleasePolicy:
        raise TrustPolicyError("externally reviewed policy required")
    # A frozen dataclass may be instantiated directly without review_policy.
    # Reject malformed snapshots even when their concrete type matches.
    review_policy(publisher=trusted_policy.publisher,
                  public_key_sha256=trusted_policy.public_key_sha256,
                  minimum_sequence=trusted_policy.minimum_sequence,
                  installed_sequence=trusted_policy.installed_sequence)
    if (type(received_public_key_sha256) is not str
            or received_public_key_sha256 != trusted_policy.public_key_sha256
            or type(signed_publisher) is not str
            or signed_publisher != trusted_policy.publisher
            or type(proposed_sequence) is not int
            or proposed_sequence < trusted_policy.minimum_sequence
            or proposed_sequence <= trusted_policy.installed_sequence
            or proposed_sequence > MAX_SEQUENCE):
        raise TrustPolicyError("untrusted key/publisher or rollback attempt")
    return {
        "status": "TRUST_POLICY_MATCH_OFFLINE_ONLY",
        "proposed_sequence": proposed_sequence,
        "deployment_authorized": False,
        "remote_authorized": False,
        "journal_write_authorized": False,
    }

def compare_journal_copy(*, protected_high_water, observed_high_water):
    """Reject observed copies below a separately protected high-water."""
    if (type(protected_high_water) is not int or
            type(observed_high_water) is not int or
            not 0 <= protected_high_water <= MAX_SEQUENCE or
            not 0 <= observed_high_water <= MAX_SEQUENCE):
        raise TrustPolicyError("invalid journal counter")
    if observed_high_water < protected_high_water:
        raise TrustPolicyError("journal rollback detected")
    return {"status": "COUNTER_COMPARED_NOT_ATTESTED",
            "deployment_authorized": False}


def review_pinned_signed_bom(*, trusted_policy, trusted_ed25519_public_key,
                             signed_bom_receipt_bytes,
                             expected_candidate_manifest_sha256):
    """Join explicit external pins to BOM v2 signature evidence, DATA_ONLY.

    Policy and expected manifest digest origins remain unproven unless
    independently verified/protected by the caller. Matching caller inputs
    cannot attest the Owner, publisher or code provenance, or grant authority.
    Returns existing HOLD_LIVE evidence with all permissions false. Policy,
    key pin and manifest errors raise TrustPolicyError; receipt/signature
    errors retain the existing BrokerBomReceiptError. No artifacts are read
    and no state is mutated.
    """
    if type(trusted_policy) is not TrustedReleasePolicy:
        raise TrustPolicyError("externally reviewed policy required")
    review_policy(publisher=trusted_policy.publisher,
                  public_key_sha256=trusted_policy.public_key_sha256,
                  minimum_sequence=trusted_policy.minimum_sequence,
                  installed_sequence=trusted_policy.installed_sequence)
    if (type(trusted_ed25519_public_key) is not bytes or
            len(trusted_ed25519_public_key) != 32 or
            sha256(trusted_ed25519_public_key).hexdigest() !=
            trusted_policy.public_key_sha256):
        raise TrustPolicyError("public key differs from external fingerprint")
    if (type(expected_candidate_manifest_sha256) is not str or
            SHA.fullmatch(expected_candidate_manifest_sha256) is None):
        raise TrustPolicyError("invalid external candidate manifest digest")
    evidence = release_broker_bom_receipt.verify_signed_receipt(
        signed_bom_receipt_bytes,
        trusted_ed25519_public_key=trusted_ed25519_public_key,
        trusted_publisher=trusted_policy.publisher,
        minimum_sequence=trusted_policy.minimum_sequence,
        installed_sequence=trusted_policy.installed_sequence)
    if evidence.candidate_manifest_sha256 != expected_candidate_manifest_sha256:
        raise TrustPolicyError("candidate manifest differs from external digest")
    return evidence


@dataclass(frozen=True)
class StableJournalReviewedBom:
    """Non-authorizing observations; never proof of independent protected origin.

    Its signed_bom has only cryptographic/structural validity under caller keys.
    The journal/floor fields are caller-supplied observations, NOT OS attestations.
    A separate protected service/install gate must establish trusted provenance,
    freshness, installed artifact bytes, and durable rollback recovery.
    """
    signed_bom: release_broker_bom_receipt.VerifiedBrokerBom
    stable_sequence: int
    stable_high_water: int
    external_floor_claim: int
    stable_journal_sha256: str
    pending_absence_claimed: bool = field(default=True, init=False)
    status: str = field(default="HOLD_LIVE", init=False)
    publisher_provenance_verified: bool = field(default=False, init=False)
    protected_floor_origin_verified: bool = field(default=False, init=False)
    deployment_authorized: bool = field(default=False, init=False)
    remote_authorized: bool = field(default=False, init=False)
    install_permitted: bool = field(default=False, init=False)

    def __post_init__(self):
        # Type guards prevent accidental forged/contradictory review results.
        # They do NOT prove signature verification, Owner provenance, or
        # integrity against code already executing in this Python process.
        if type(self.signed_bom) is not release_broker_bom_receipt.VerifiedBrokerBom:
            raise TrustPolicyError("invalid signed BOM evidence type")
        if (type(self.stable_sequence) is not int
                or type(self.stable_high_water) is not int
                or type(self.external_floor_claim) is not int
                or not 0 <= self.stable_sequence <= MAX_SEQUENCE
                or self.stable_sequence != self.stable_high_water
                or self.stable_sequence != self.external_floor_claim
                or type(self.signed_bom.sequence) is not int
                or not self.stable_sequence < self.signed_bom.sequence <= MAX_SEQUENCE
                or type(self.stable_journal_sha256) is not str
                or SHA.fullmatch(self.stable_journal_sha256) is None
                or self.signed_bom.status != "HOLD_LIVE"
                or any(getattr(self.signed_bom, flag) is not False for flag in (
                    "deployment_authorized", "remote_authorized",
                    "code_provenance_authenticated",
                    "external_trust_anchor_origin_verified", "install_permitted"))):
            raise TrustPolicyError("inconsistent journal review observations")


def review_signed_bom_against_stable_journal(
        *, trusted_policy, trusted_ed25519_public_key,
        signed_bom_receipt_bytes, expected_candidate_manifest_sha256,
        stable_journal_bytes, external_protected_floor, pending_journal_bytes):
    """Join G1 BOM verification to an externally protected stable high-water.

    Offline READ-ONLY, DATA_ONLY / HOLD_LIVE. The *caller* must establish the
    independent OS-protected origin and freshness of external_protected_floor,
    public-key pin, policy and manifest digest. Bytes from the same candidate
    channel do not prove provenance. This function NEVER reads/writes disk,
    advances a journal, grants install/remote, or performs crash recovery.

    Only a fully stable checkpoint with no pending transaction is accepted.
    pending_journal_bytes is REQUIRED: None explicitly means the caller
    claims to have inspected the pending location and found it absent.
    Neither None nor the returned object attests that inspection. A prepared
    transaction remains HOLD pending separately accepted service recovery.

    Counter equality detects an *inconsistent* reset only when the external
    floor actually comes from an independent protected source. If the journal,
    floor, and policy are consistently rolled back together, a formerly signed
    older BOM can still pass. This is NOT a durable security boundary.
    Legacy journal records have no product/publisher/manifest/generation
    binding; those must be authenticated by a future protected service.
    """
    try:
        from .release_journal_recovery import JournalRecoveryError, parse_record
    except ImportError as exc:
        raise TrustPolicyError("release journal parser unavailable") from exc

    if type(trusted_policy) is not TrustedReleasePolicy:
        raise TrustPolicyError("externally reviewed policy required")
    review_policy(
        publisher=trusted_policy.publisher,
        public_key_sha256=trusted_policy.public_key_sha256,
        minimum_sequence=trusted_policy.minimum_sequence,
        installed_sequence=trusted_policy.installed_sequence)
    if (type(external_protected_floor) is not int
            or not 0 <= external_protected_floor <= MAX_SEQUENCE):
        raise TrustPolicyError("invalid external protected high-water")
    if pending_journal_bytes is not None:
        # Never guess which transaction completed after a crash.
        raise TrustPolicyError("pending release transaction requires Owner recovery")
    try:
        stable = parse_record(stable_journal_bytes)
    except JournalRecoveryError as exc:
        raise TrustPolicyError("invalid stable release journal") from exc
    if (stable["phase"] != "STABLE"
            or stable["sequence"] != stable["high_water"]
            or stable["high_water"] != external_protected_floor
            or trusted_policy.installed_sequence != external_protected_floor):
        raise TrustPolicyError("journal/policy high-water disagreement or pending state")

    # With the counters aligned, retain existing Ed25519, publisher,
    # fingerprint, signed manifest and strict rollback checks unchanged.
    # Distinguish this data-only observation from a standalone signature
    # check. Neither type can be used as install/remote authorization.
    signed = review_pinned_signed_bom(
        trusted_policy=trusted_policy,
        trusted_ed25519_public_key=trusted_ed25519_public_key,
        signed_bom_receipt_bytes=signed_bom_receipt_bytes,
        expected_candidate_manifest_sha256=expected_candidate_manifest_sha256)
    return StableJournalReviewedBom(
        signed_bom=signed, stable_sequence=stable["sequence"],
        stable_high_water=stable["high_water"],
        external_floor_claim=external_protected_floor,
        stable_journal_sha256=sha256(stable_journal_bytes).hexdigest())
