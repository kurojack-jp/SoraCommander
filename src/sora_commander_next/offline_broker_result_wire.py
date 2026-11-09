"""Offline-only, read-only result wire candidate. NO listener or authority.

Not exported over MCP or Windows IPC. A trusted Broker must independently
authenticate the live client before ever calling this data formatter.
"""
import base64
import binascii
import json
import re

MAX_RESULT_FRAME = 8192
MAX_RAW_OUTPUT = 4096  # binary payload; base64 and metadata fit in one frame
_REQUEST = re.compile(r"[0-9a-f]{32}")
_SESSION = re.compile(r"[0-9a-f]{48}")
_ALLOWED_STATUS = frozenset((
    "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
    "OUTPUT_LIMIT", "OUTPUT_INCOMPLETE", "ERROR", "OUTCOME_UNKNOWN",
    "INTERRUPTED_UNKNOWN",
))


class ResultWireRejected(ValueError):
    pass


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ResultWireRejected("duplicate field")
        result[key] = value
    return result


def _decode(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RESULT_FRAME:
        raise ResultWireRejected("invalid frame size")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              ResultWireRejected("nonfinite value")))
    except (UnicodeError, ValueError) as exc:
        raise ResultWireRejected("invalid json") from exc


def decode_read_request(raw):
    """Strict schema: never accepts approve, submit, retry or command args."""
    msg = _decode(raw)
    if (type(msg) is not dict or set(msg) !=
            {"version", "op", "request_id", "session_id", "offset"}
            or type(msg["version"]) is not int or msg["version"] != 2
            or msg["op"] != "read_result"
            or type(msg["request_id"]) is not str
            or _REQUEST.fullmatch(msg["request_id"]) is None
            or type(msg["session_id"]) is not str
            or _SESSION.fullmatch(msg["session_id"]) is None
            or type(msg["offset"]) is not int
            or not 0 <= msg["offset"] <= 65536):
        raise ResultWireRejected("invalid read-only request")
    return msg


def encode_read_result(*, request_id, session_id, status, exit_code,
                       stdout=b"", stderr=b"", offset=0, next_offset=0,
                       captured_bytes=0, output_incomplete=False,
                       archived_truncated=False):
    """Copy bounded opaque bytes; never expose argv, paths, env or grants.

    offset/next_offset refer to the underlying captured byte stream, not
    lengths of the independently grouped stdout and stderr representations.
    """
    if (type(request_id) is not str or _REQUEST.fullmatch(request_id) is None
            or type(session_id) is not str or _SESSION.fullmatch(session_id) is None
            or type(status) is not str or status not in _ALLOWED_STATUS
            or (exit_code is not None and
                (type(exit_code) is not int or not -(2**31) <= exit_code <= 2**32-1))
            or type(stdout) is not bytes or type(stderr) is not bytes
            or len(stdout) + len(stderr) > MAX_RAW_OUTPUT
            or type(offset) is not int or type(next_offset) is not int
            or type(captured_bytes) is not int
            or not 0 <= offset <= next_offset <= captured_bytes <= 65536
            or type(output_incomplete) is not bool
            or type(archived_truncated) is not bool
            or (status in ("RUNNING", "INTERRUPTED_UNKNOWN") and exit_code is not None)):
        raise ResultWireRejected("invalid result data")
    msg = {
        "version": 2, "op": "read_result", "request_id": request_id,
        "session_id": session_id, "status": status, "exit_code": exit_code,
        "offset": offset, "next_offset": next_offset,
        "captured_bytes": captured_bytes,
        "output_incomplete": output_incomplete,
        "archived_truncated": archived_truncated,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
    }
    raw = json.dumps(msg, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(raw) > MAX_RESULT_FRAME:
        raise ResultWireRejected("result frame exceeds limit")
    return raw


def decode_read_result(raw):
    msg = _decode(raw)
    expected = {"version", "op", "request_id", "session_id", "status",
                "exit_code", "offset", "next_offset", "captured_bytes",
                "output_incomplete", "archived_truncated",
                "stdout_base64", "stderr_base64"}
    if type(msg) is not dict or set(msg) != expected:
        raise ResultWireRejected("invalid result schema")
    for k in ("stdout_base64", "stderr_base64"):
        if type(msg[k]) is not str:
            raise ResultWireRejected("invalid binary output")
        try:
            base64.b64decode(msg[k], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ResultWireRejected("invalid binary output") from exc
    normalized = encode_read_result(
        request_id=msg["request_id"], session_id=msg["session_id"],
        status=msg["status"], exit_code=msg["exit_code"],
        stdout=base64.b64decode(msg["stdout_base64"]),
        stderr=base64.b64decode(msg["stderr_base64"]),
        offset=msg["offset"], next_offset=msg["next_offset"],
        captured_bytes=msg["captured_bytes"],
        output_incomplete=msg["output_incomplete"],
        archived_truncated=msg["archived_truncated"])
    if normalized != raw:
        raise ResultWireRejected("noncanonical result")
    return msg
