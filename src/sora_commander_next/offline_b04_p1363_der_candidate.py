"""DEV_ONLY: strict P-256 raw r||s to canonical DER. Never grants authentication."""
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature, decode_dss_signature

P256_ORDER = int("ffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551", 16)


class SignatureFormatDenied(ValueError):
    pass


def p1363_to_der(raw: bytes) -> bytes:
    if type(raw) is not bytes or len(raw) != 64:
        raise SignatureFormatDenied("INVALID_P1363_LENGTH")
    r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    if not 1 <= r < P256_ORDER or not 1 <= s <= P256_ORDER // 2:
        raise SignatureFormatDenied("INVALID_OR_NONCANONICAL_SCALAR")
    return encode_dss_signature(r, s)


def der_to_p1363(der: bytes) -> bytes:
    if type(der) is not bytes or not der or len(der) > 72:
        raise SignatureFormatDenied("INVALID_DER_LENGTH")
    try:
        r, s = decode_dss_signature(der)
    except ValueError as exc:
        raise SignatureFormatDenied("INVALID_DER") from exc
    if not 1 <= r < P256_ORDER or not 1 <= s <= P256_ORDER // 2:
        raise SignatureFormatDenied("INVALID_OR_NONCANONICAL_SCALAR")
    if encode_dss_signature(r, s) != der:
        raise SignatureFormatDenied("NONCANONICAL_DER")
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")
