"""DEV_ONLY G1 protected ledger v2 WIRE SHAPE review.

NO signature verification, service identity, TPM witness, Owner authentication,
journal reads/writes, key creation, installation, launch or remote permission.
This parser checks untrusted bytes, not their provenance. Never import in
product composition or treat its results as authority.
"""
from dataclasses import dataclass, field
from types import MappingProxyType
from hashlib import sha256
import json
import re

FORMAT = "sora-commander-next-protected-ledger-v2"
PRODUCT = "sora-commander-next"
DOMAIN = b"SoraCommanderNext Protected Ledger v2\x00"
MAX_RECORD_BYTES = 4096
MAX_RECORDS = 256
MAX_SEQUENCE = (1 << 63) - 1
ZERO = "0" * 64
GENESIS_TRANSACTION = "0" * 32
SHA = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[0-9a-f]{32}\Z")
PUBLISHER = re.compile(r"[a-z0-9_][a-z0-9_-]{2,63}\Z")
KEY = re.compile(r"[a-z0-9_][a-z0-9_.-]{2,63}\Z")
SIG = re.compile(r"[0-9a-f]{128}\Z")  # P256 IEEE P1363 r||s syntax only
PHASES = frozenset(("GENESIS", "PREPARED", "COMMITTED", "ACTIVE", "ABORTED"))
FIELDS = frozenset((
    "format", "product", "publisher", "publisher_key_id", "service_key_id",
    "generation_id", "record_index", "phase", "sequence",
    "candidate_manifest_sha256", "installed_manifest_sha256",
    "previous_record_sha256", "transaction_id", "signature_hex",
))
PAYLOAD_FIELDS = FIELDS - {"signature_hex"}


class LedgerV2ShapeError(ValueError):
    """Malformed/unbound input. Not a signature or Owner-auth error."""


@dataclass(frozen=True)
class LedgerV2Shape:
    """Untrusted structural observation, never an authenticated receipt."""
    record: dict
    canonical_sha256: str
    signing_bytes_sha256: str
    status: str = field(default="DEV_ONLY_SHAPE_UNAUTHENTICATED", init=False)
    signature_verified: bool = field(default=False, init=False)
    origin_verified: bool = field(default=False, init=False)
    high_water_protected: bool = field(default=False, init=False)
    install_permitted: bool = field(default=False, init=False)
    remote_permitted: bool = field(default=False, init=False)


@dataclass(frozen=True)
class LedgerV2ChainShape:
    record_count: int
    generation_id: str
    last_record_index: int
    last_phase: str
    last_sequence: int
    status: str = field(default="DEV_ONLY_CHAIN_UNAUTHENTICATED_HOLD", init=False)
    signature_verified: bool = field(default=False, init=False)
    antirollback_verified: bool = field(default=False, init=False)
    install_permitted: bool = field(default=False, init=False)
    remote_permitted: bool = field(default=False, init=False)


def _canonical(obj):
    return (json.dumps(obj, sort_keys=True, ensure_ascii=True,
                       allow_nan=False, separators=(",", ":")) + "\n").encode("ascii")


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise LedgerV2ShapeError("duplicate v2 field")
        result[name] = value
    return result


def _reject_constant(value):
    raise LedgerV2ShapeError("nonfinite JSON constant forbidden")


def _validate(record):
    if type(record) is not dict or set(record) != FIELDS:
        raise LedgerV2ShapeError("exact v2 schema required")
    if record["format"] != FORMAT or type(record["format"]) is not str:
        raise LedgerV2ShapeError("incorrect v2 format")
    if record["product"] != PRODUCT or type(record["product"]) is not str:
        raise LedgerV2ShapeError("unexpected product")
    for key, regex in (
            ("publisher", PUBLISHER), ("publisher_key_id", KEY),
            ("service_key_id", KEY), ("generation_id", ID),
            ("transaction_id", ID), ("candidate_manifest_sha256", SHA),
            ("installed_manifest_sha256", SHA),
            ("previous_record_sha256", SHA), ("signature_hex", SIG)):
        v = record[key]
        if type(v) is not str or regex.fullmatch(v) is None:
            raise LedgerV2ShapeError("malformed field: " + key)
    for key, lower in (("record_index", 0), ("sequence", 0)):
        v = record[key]
        if type(v) is not int or not lower <= v <= MAX_SEQUENCE:
            raise LedgerV2ShapeError("invalid bounded integer: " + key)
    if type(record["phase"]) is not str or record["phase"] not in PHASES:
        raise LedgerV2ShapeError("unrecognized v2 phase")
    if record["phase"] == "GENESIS":
        if (record["record_index"] != 0 or record["sequence"] != 0
                or record["previous_record_sha256"] != ZERO
                or record["transaction_id"] != GENESIS_TRANSACTION):
            raise LedgerV2ShapeError("unanchored genesis shape")
    elif (record["record_index"] == 0 or record["sequence"] == 0
          or record["transaction_id"] == GENESIS_TRANSACTION
          or record["previous_record_sha256"] == ZERO):
        raise LedgerV2ShapeError("non-genesis record cannot use genesis markers")


def parse_v2_record(raw):
    """Parse only exact canonical v2 bytes; DO NOT verify P256 signature."""
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_RECORD_BYTES:
        raise LedgerV2ShapeError("bounded byte record required")
    try:
        parsed = json.loads(raw.decode("ascii"), object_pairs_hook=_unique,
                            parse_constant=_reject_constant)
        _validate(parsed)
        if raw != _canonical(parsed):
            raise LedgerV2ShapeError("noncanonical v2 record")
        payload = {k: parsed[k] for k in PAYLOAD_FIELDS}
        to_sign = DOMAIN + _canonical(payload)
    except (UnicodeError, ValueError, TypeError, OverflowError) as exc:
        raise LedgerV2ShapeError("invalid or noncanonical v2 record") from exc
    # Do not expose a mutable parsed dictionary as a frozen observation.
    # This is API hygiene only; the observation itself is unauthenticated.
    return LedgerV2Shape(MappingProxyType(parsed), sha256(raw).hexdigest(),
                         sha256(to_sign).hexdigest())


def review_v2_chain_shape(raw_records):
    """Bounded in-memory sequence checks; does not authenticate signer/floor.

    Genesis is not independently trusted by this function. A fully consistent
    forged chain can pass this review. No service may use this as a grant.
    """
    if type(raw_records) is not tuple or not 1 <= len(raw_records) <= MAX_RECORDS:
        raise LedgerV2ShapeError("bounded tuple of v2 records required")
    records = [parse_v2_record(x) for x in raw_records]
    first = records[0].record
    if first["phase"] != "GENESIS":
        raise LedgerV2ShapeError("v2 chain must start at genesis")
    previous = records[0]
    for i, observed in enumerate(records[1:], 1):
        item = observed.record
        earlier = previous.record
        if (item["record_index"] != i or
                item["previous_record_sha256"] != previous.canonical_sha256 or
                any(item[k] != first[k] for k in (
                    "product", "publisher", "publisher_key_id",
                    "service_key_id", "generation_id"))):
            raise LedgerV2ShapeError("v2 index, chain or identity mismatch")
        prev_phase, next_phase = earlier["phase"], item["phase"]
        if prev_phase in ("GENESIS", "ACTIVE", "ABORTED"):
            if next_phase != "PREPARED" or item["sequence"] <= earlier["sequence"]:
                raise LedgerV2ShapeError("new generation step must prepare higher sequence")
        elif prev_phase == "PREPARED":
            if next_phase not in ("COMMITTED", "ABORTED"):
                raise LedgerV2ShapeError("prepared may only commit or abort")
        elif prev_phase == "COMMITTED":
            if next_phase != "ACTIVE":
                raise LedgerV2ShapeError("committed may only become active")
        else:
            raise LedgerV2ShapeError("unexpected prior phase")
        if prev_phase in ("PREPARED", "COMMITTED"):
            if (item["transaction_id"] != earlier["transaction_id"]
                    or item["sequence"] != earlier["sequence"]
                    or item["candidate_manifest_sha256"] != earlier["candidate_manifest_sha256"]
                    or item["installed_manifest_sha256"] != earlier["installed_manifest_sha256"]):
                raise LedgerV2ShapeError("transaction pin changed")
        else:
            if item["transaction_id"] == earlier["transaction_id"]:
                raise LedgerV2ShapeError("transaction id reused")
        previous = observed
    end = previous.record
    return LedgerV2ChainShape(len(records), first["generation_id"],
                              end["record_index"], end["phase"], end["sequence"])
