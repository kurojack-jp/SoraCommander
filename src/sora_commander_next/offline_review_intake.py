"""INERT test intake for a future single Owner-owned review queue.

This accepts untrusted DATA ONLY messages and never authenticates Windows
tokens, approves work, opens a GUI, spawns a job, grants permission, or starts
any IPC listener. In particular, client-provided scope hashes are merely
untrusted labels and MUST NOT be used to authorize a command.
"""
import json
import re
from .offline_review_queue import OfflineReviewQueue

_MAX_FRAME = 2048
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_STATUSES = frozenset((
    "OK_DATA_ONLY", "QUEUED_DATA_ONLY", "IN_REVIEW_DATA_ONLY",
    "DENIED_DATA_ONLY", "REVIEWED_DATA_ONLY", "UNKNOWN_DATA_ONLY",
    "UNKNOWN", "DENY_CONFLICT", "DENY_INVALID", "DENY_CLOSED", "BUSY",
    "DENY_FRAME", "ERROR_DATA_ONLY",
))


def _unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _decode(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= _MAX_FRAME:
        raise ValueError("invalid frame size")
    value = json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=_unique_fields,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    if type(value) is not dict or value.get("version") != 1 or type(value.get("version")) is not int:
        raise ValueError("invalid version")
    op = value.get("op")
    if type(op) is not str or op not in ("health", "submit", "observe"):
        raise ValueError("unsupported operation")
    if op == "health":
        if set(value) != {"version", "op"}:
            raise ValueError("bad health fields")
    else:
        if set(value) != {"version", "op", "request_id", "scope_sha256"}:
            raise ValueError("bad review fields")
        if (type(value["request_id"]) is not str
                or _ID.fullmatch(value["request_id"]) is None
                or type(value["scope_sha256"]) is not str
                or _SHA.fullmatch(value["scope_sha256"]) is None):
            raise ValueError("malformed request labels")
    return value


class OfflineReviewIntake:
    """No trusted transport: a data-only serializer over ONE existing queue.

    This may be invoked from multiple *test* threads or processes forwarding
    data to the one owning process. It is never an Owner authentication gate.
    Queue admission and approval are different concepts: no real consent here.
    """

    def __init__(self, queue):
        if type(queue) is not OfflineReviewQueue:
            raise TypeError("exact inert queue instance required")
        self._queue = queue

    def handle_data_only(self, raw):
        """Parse one data frame, submit/observe, and return bounded JSON bytes.

        Never returns queue_id (the internal completion ticket), and never
        exposes a complete/review/approve/revoke/start operation to clients.
        """
        rid = None
        try:
            msg = _decode(raw)
            rid = msg.get("request_id")
            if msg["op"] == "health":
                status = "OK_DATA_ONLY"
            elif msg["op"] == "submit":
                status = self._queue.submit_data_only(
                    rid, msg["scope_sha256"]).status
            else:
                status = self._queue.observe_data_only(
                    rid, msg["scope_sha256"]).status
            if status not in _STATUSES:
                raise ValueError("unknown queue status")
        except (ValueError, TypeError, UnicodeError, OverflowError):
            status = "DENY_FRAME"
            rid = None
        except Exception:
            # Even an unexpected queue fault is an inert, bounded error.
            # Do not reveal stack traces, private queue state, or approvals.
            status = "ERROR_DATA_ONLY"
            rid = None
        response = {
            "version": 1, "status": status, "request_id": rid,
        }
        return json.dumps(
            response, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False).encode("ascii")
