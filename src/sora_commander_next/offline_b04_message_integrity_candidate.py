"""DEV_ONLY bounded message MAC/sequence exercise. NOT an authenticated channel.

The HMAC key is caller supplied and UNTRUSTED. Passing this exercise never
enables admission; a real installer-owned trust anchor and OS channel binding
are mandatory before any production integration.
"""
from dataclasses import dataclass
import hashlib
import hmac
import json
import re

HEX64 = re.compile(r"[0-9a-f]{64}\Z")
DIRECTIONS = frozenset(("client-to-service", "service-to-client"))
DOMAIN = b"SCN/B04/offline-message-integrity/v1\x00"
MAX_PAYLOAD = 4096
MAX_SEQUENCE = (1 << 63) - 1


class MessageEvidenceDenied(ValueError):
    pass


def _valid_hex(value):
    return type(value) is str and HEX64.fullmatch(value) is not None


@dataclass(frozen=True)
class MessageEnvelope:
    epoch: int
    direction: str
    sequence: int
    client_nonce: str
    server_nonce: str
    payload: bytes

    def __post_init__(self):
        if type(self.epoch) is not int or not 0 < self.epoch <= MAX_SEQUENCE:
            raise MessageEvidenceDenied("INVALID_EPOCH")
        if self.direction not in DIRECTIONS or type(self.direction) is not str:
            raise MessageEvidenceDenied("INVALID_DIRECTION")
        if type(self.sequence) is not int or not 1 <= self.sequence <= MAX_SEQUENCE:
            raise MessageEvidenceDenied("INVALID_SEQUENCE")
        if not _valid_hex(self.client_nonce) or not _valid_hex(self.server_nonce):
            raise MessageEvidenceDenied("INVALID_NONCE")
        if self.client_nonce == self.server_nonce:
            raise MessageEvidenceDenied("NONCE_COLLISION")
        if type(self.payload) is not bytes or len(self.payload) > MAX_PAYLOAD:
            raise MessageEvidenceDenied("INVALID_PAYLOAD")


def _bytes(env):
    if type(env) is not MessageEnvelope:
        raise MessageEvidenceDenied("INVALID_ENVELOPE")
    metadata = dict(epoch=env.epoch, direction=env.direction,
                    sequence=env.sequence, client_nonce=env.client_nonce,
                    server_nonce=env.server_nonce,
                    payload_sha256=hashlib.sha256(env.payload).hexdigest(),
                    payload_length=len(env.payload))
    return DOMAIN + json.dumps(metadata, sort_keys=True,
                              separators=(",", ":"), ensure_ascii=True).encode()


def demo_mac(key, envelope):
    if type(key) is not bytes or len(key) != 32:
        raise MessageEvidenceDenied("INVALID_DEMO_KEY")
    return hmac.digest(key, _bytes(envelope), "sha256")


@dataclass(frozen=True)
class MessageEvidenceReview:
    mac_matches: bool
    next_sequence: bool
    authentication: str = "NOT_AUTHENTICATED"
    admission: str = "UNAVAILABLE"


class OfflineSequenceReview:
    """In-memory, test-only sequence tracker. No durable anti-replay claim."""
    def __init__(self, *, epoch, client_nonce, server_nonce):
        if type(epoch) is not int or not 0 < epoch <= MAX_SEQUENCE:
            raise MessageEvidenceDenied("INVALID_EPOCH")
        if not _valid_hex(client_nonce) or not _valid_hex(server_nonce) or client_nonce == server_nonce:
            raise MessageEvidenceDenied("INVALID_NONCE")
        self.epoch = epoch
        self.client_nonce = client_nonce
        self.server_nonce = server_nonce
        self._last = {direction: 0 for direction in DIRECTIONS}

    def review(self, key, env, mac):
        expected = demo_mac(key, env)
        if type(mac) is not bytes or len(mac) != 32:
            raise MessageEvidenceDenied("INVALID_MAC")
        matching_context = (env.epoch == self.epoch and
                            env.client_nonce == self.client_nonce and
                            env.server_nonce == self.server_nonce)
        matching_mac = matching_context and hmac.compare_digest(expected, mac)
        next_seq = matching_mac and self._last[env.direction] < MAX_SEQUENCE and env.sequence == self._last[env.direction] + 1
        if next_seq:
            self._last[env.direction] = env.sequence
        return MessageEvidenceReview(bool(matching_mac), bool(next_seq))
