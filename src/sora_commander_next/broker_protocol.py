"""Strict bounded broker message codec; no listener, socket, or authority."""
import base64
import binascii
import json
import re

MAX_FRAME = 8192
OPERATIONS = frozenset(("health", "query", "cancel"))
TOKEN = re.compile(r"[0-9a-f]{32}")


class ProtocolRejected(ValueError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolRejected("duplicate field")
        result[key] = value
    return result


def decode_request(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_FRAME:
        raise ProtocolRejected("invalid frame size")
    try:
        msg = json.loads(raw.decode("utf-8", errors="strict"),
                         object_pairs_hook=_unique,
                         parse_constant=lambda _: (_ for _ in ()).throw(ProtocolRejected("nonfinite value")))
    except (UnicodeError, ValueError) as exc:
        raise ProtocolRejected("invalid frame") from exc
    if type(msg) is not dict or type(msg.get("version")) is not int or msg["version"] != 1 or type(msg.get("op")) is not str:
        raise ProtocolRejected("invalid schema")
    op = msg["op"]
    if op not in OPERATIONS:
        raise ProtocolRejected("unsupported operation")
    expected = {"version", "op"} if op == "health" else {"version", "op", "request_id"}
    if set(msg) != expected:
        raise ProtocolRejected("unexpected fields")
    if op != "health" and (type(msg["request_id"]) is not str or TOKEN.fullmatch(msg["request_id"]) is None):
        raise ProtocolRejected("invalid request ID")
    return msg


def encode_response(msg):
    if type(msg) is not dict or set(msg) != {"version", "status", "request_id"}:
        raise ProtocolRejected("invalid response")
    if type(msg["version"]) is not int or msg["version"] != 1 or type(msg["status"]) is not str or msg["status"] not in ("OK", "UNKNOWN", "DENIED", "ERROR"):
        raise ProtocolRejected("invalid response")
    if msg["request_id"] is not None and (
        type(msg["request_id"]) is not str or TOKEN.fullmatch(msg["request_id"]) is None
    ):
        raise ProtocolRejected("invalid response ID")
    raw = json.dumps(msg, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    if len(raw) > MAX_FRAME:
        raise ProtocolRejected("oversized response")
    return raw


# Explicit v2 data codec. The v1 functions/operations above remain separate.
_RESULT_KEYS = frozenset(("request_id", "status"))
_PAGE_KEYS = _RESULT_KEYS | frozenset((
    "session_id", "exit_code", "offset", "next_offset", "captured_bytes",
    "output_incomplete", "overflow", "end_of_capture", "stdout_base64",
    "stderr_base64",
))
_PAGE_STATES = frozenset((
    "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUTPUT_LIMIT",
    "OUTPUT_INCOMPLETE", "ERROR", "OUTCOME_UNKNOWN",
))
_SESSION_TOKEN = re.compile(r"[0-9a-f]{48}")


def decode_result_page_request(raw):
    """Decode only a bounded v2 read; IDs confer no authority."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_FRAME:
        raise ProtocolRejected("invalid result frame size")
    try:
        msg = json.loads(
            raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ProtocolRejected("nonfinite value")))
    except (UnicodeError, ValueError, RecursionError):
        raise ProtocolRejected("invalid result frame") from None
    if (type(msg) is not dict
            or set(msg) != {"version", "op", "request_id", "offset"}
            or type(msg["version"]) is not int or msg["version"] != 2
            or type(msg["op"]) is not str or msg["op"] != "read_result"
            or type(msg["request_id"]) is not str
            or TOKEN.fullmatch(msg["request_id"]) is None
            or type(msg["offset"]) is not int
            or not 0 <= msg["offset"] <= 65536):
        raise ProtocolRejected("invalid result request schema")
    return msg


def _validate_result_page(data):
    """Validate a detached query_result_page snapshot before serialization."""
    if (type(data["session_id"]) is not str
            or _SESSION_TOKEN.fullmatch(data["session_id"]) is None
            or data["status"] not in _PAGE_STATES):
        raise ProtocolRejected("invalid result page identity or state")
    status, code = data["status"], data["exit_code"]
    if (code is not None and (type(code) is not int
                             or not -(2**31) <= code <= 2**32 - 1)):
        raise ProtocolRejected("invalid result exit code")
    if ((status == "RUNNING" and code is not None)
            or (status == "COMPLETED" and code != 0)
            or (status == "FAILED" and (code is None or code == 0))):
        raise ProtocolRejected("inconsistent result state")
    if any(type(data[key]) is not int or not 0 <= data[key] <= 65536
           for key in ("offset", "next_offset", "captured_bytes")):
        raise ProtocolRejected("invalid result offsets")
    offset, end, captured = data["offset"], data["next_offset"], data["captured_bytes"]
    if offset > captured or end != min(captured, offset + 4096):
        raise ProtocolRejected("inconsistent result offsets")
    if (any(type(data[key]) is not bool for key in (
            "output_incomplete", "overflow", "end_of_capture"))
            or data["end_of_capture"] != (end == captured)):
        raise ProtocolRejected("invalid result flags")
    if status == "COMPLETED" and (data["output_incomplete"] or data["overflow"]):
        raise ProtocolRejected("incomplete capture cannot be completed")
    size = 0
    for key in ("stdout_base64", "stderr_base64"):
        encoded = data[key]
        if type(encoded) is not str or len(encoded) > 5464:
            raise ProtocolRejected("invalid result encoding size")
        try:
            binary = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ProtocolRejected("invalid result encoding") from None
        if base64.b64encode(binary).decode("ascii") != encoded:
            raise ProtocolRejected("noncanonical result encoding")
        size += len(binary)
        if size > 4096:
            raise ProtocolRejected("oversized result page")
    if size != end - offset:
        raise ProtocolRejected("inconsistent result size")


def encode_result_page_response(data):
    """Encode query_result_page data, never approval or arbitrary metadata.

    UNKNOWN/OUTCOME_UNKNOWN disclose only request ID and state. This helper
    validates data, not ownership; the in-process runtime supplies the guard.
    """
    if type(data) is not dict or set(data) not in (_RESULT_KEYS, _PAGE_KEYS):
        raise ProtocolRejected("invalid result response schema")
    data = dict(data)
    if (type(data["request_id"]) is not str
            or TOKEN.fullmatch(data["request_id"]) is None
            or type(data["status"]) is not str):
        raise ProtocolRejected("invalid result response identity or state")
    if set(data) == _PAGE_KEYS:
        _validate_result_page(data)
    elif data["status"] not in ("UNKNOWN", "OUTCOME_UNKNOWN"):
        raise ProtocolRejected("missing result page")
    if data["status"] in ("UNKNOWN", "OUTCOME_UNKNOWN"):
        data = {key: data[key] for key in _RESULT_KEYS}
    raw = json.dumps({"version": 2, "op": "read_result", **data},
                     sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                     allow_nan=False).encode("ascii")
    if len(raw) > MAX_FRAME:
        raise ProtocolRejected("oversized result response")
    return raw
