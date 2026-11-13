"""DEV_ONLY Broker side of the Host-to-Broker wire. Not connected to any listener.

The Host keeps the MCP connection; this object runs in a separate Owner process
and is the only place that reaches the backend which holds jobs and the Owner
review window. Authentication belongs to the transport that calls handle().

There is deliberately no approval action, and no fallback or retry here. A
failed or oversized reply is reported as OUTCOME_UNKNOWN, never as not started.
"""
import math
import re
import threading

from .broker_host import MAX_REPLY_BYTES, decode_frame, encode_frame
from .no_answer import valid_no_answer

_HEX32 = re.compile("[0-9a-f]{32}")
_HEX48 = re.compile("[0-9a-f]{48}")
_SHA256 = re.compile("[0-9a-f]{64}")
_MAX_SEEN_CALLS = 65536
_COMMAND_REQUIRED = frozenset({"executable", "argv", "cwd", "request_id"})
_COMMAND_OPTIONAL = frozenset({"timeout", "output_limit"})
_WAIT = frozenset({"wait_seconds"})
_LISTINGS = frozenset({"list_sessions", "previous_sessions"})
_NONE = frozenset()

# action: (required fields, optional fields)
_SCHEMAS = {
    "activity": (frozenset({"mode"}), frozenset({"id", "limit"})),
    "run_command": (_COMMAND_REQUIRED | {"background"}, _COMMAND_OPTIONAL),
    "run_command_wait": (_COMMAND_REQUIRED, _COMMAND_OPTIONAL | _WAIT),
    "await_command_result": (frozenset({"pending_id"}), _WAIT),
    "review_status": (frozenset({"pending_id"}), frozenset()),
    "request_status": (frozenset({"request_id"}), frozenset()),
    "read_process_output": (frozenset({"session_id"}), frozenset({"offset"})),
    "await_process_output": (frozenset({"session_id"}), _WAIT),
    "stop_process": (frozenset({"session_id"}), frozenset()),
    "stop_process_wait": (frozenset({"session_id"}), _WAIT),
    "list_sessions": (frozenset(), frozenset()),
    "previous_sessions": (frozenset(), frozenset()),
    "previous_output": (frozenset(), frozenset({"session_id"})),
    "status": (_NONE, _NONE),
    "recovery_overview": (_NONE, _NONE),
    "create_directory": (frozenset({"path"}), _NONE),
    "create_file": (frozenset({"path", "content"}), _NONE),
    "move_file": (frozenset({"source", "destination"}), _NONE),
    "request_source_patch_dev": (
        frozenset({"path", "old_text", "new_text", "expected_sha256"}), _NONE),
    "request_text_edit": (frozenset({"path", "content", "expected_sha256"}), _NONE),
    "request_text_create": (frozenset({"path", "content"}), _NONE),
    "edit_review_status": (frozenset({"pending_id"}), _NONE),
}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _valid_fields(arguments):
    """Exact wire types. The backend still applies its own full validation."""
    for key, value in arguments.items():
        if key in ("executable", "cwd", "path", "source", "destination"):
            valid = type(value) is str and bool(value) and "\0" not in value
        elif key == "argv":
            valid = type(value) is list and all(type(item) is str for item in value)
        elif key == "request_id":
            valid = type(value) is str and _HEX32.fullmatch(value) is not None
        elif key == "pending_id":
            valid = type(value) is str and 0 < len(value) <= 128 and value.isascii()
        elif key == "session_id":
            valid = type(value) is str and _HEX48.fullmatch(value) is not None
        elif key in ("content", "old_text", "new_text"):
            valid = type(value) is str
        elif key == "expected_sha256":
            valid = type(value) is str and _SHA256.fullmatch(value) is not None
        elif key == "background":
            valid = type(value) is bool
        elif key == "offset":
            valid = type(value) is int and value >= 0
        elif key == "output_limit":
            valid = type(value) is int
        else:  # timeout and wait_seconds
            valid = _number(value)
        if not valid:
            return False
    return True


class OwnerBrokerService:
    """Translate one canonical request frame into one backend call."""

    def __init__(self, backend):
        for action in _SCHEMAS:
            if not callable(getattr(backend, action, None)):
                raise TypeError("backend is missing a Broker action")
        self._backend = backend
        self._seen_calls = set()
        self._seen_lock = threading.Lock()

    def handle(self, raw):
        try:
            request = decode_frame(raw, max_bytes=MAX_REPLY_BYTES)
            action, call_id, arguments = self._parse(request)
        except (ValueError, TypeError, UnicodeError):
            # No bound envelope exists, so a client must treat this as a failure.
            return encode_frame({"v": 1, "action": "invalid", "call_id": "0" * 32,
                                 "result": {"status": "DENIED",
                                            "error": "InvalidBrokerRequest"}})
        return self._reply(action, call_id, self._result(action, call_id, arguments))

    @staticmethod
    def _parse(request):
        if set(request) != {"v", "action", "call_id", "arguments"}:
            raise ValueError("unknown envelope")
        action, call_id, arguments = (request["action"], request["call_id"],
                                      request["arguments"])
        if (type(request["v"]) is not int or request["v"] != 1
                or type(action) is not str
                or type(call_id) is not str or _HEX32.fullmatch(call_id) is None
                or type(arguments) is not dict):
            raise ValueError("invalid envelope")
        return action, call_id, arguments

    def _result(self, action, call_id, arguments):
        schema = _SCHEMAS.get(action)
        if schema is None:
            return {"status": "DENIED", "error": "ActionNotAllowed"}
        required, optional = schema
        names = set(arguments)
        if not required <= names or not names <= required | optional:
            if action == "activity":
                from .activity import reply as activity_reply
                return activity_reply(status="ERROR", error="InvalidArguments", action="CORRECT_READ_ARGUMENTS")
            return {"status": "DENIED", "error": "InvalidBrokerArguments"}
        if action == "activity":
            from .activity import arguments as activity_arguments, reply as activity_reply
            try:
                activity_arguments(arguments)
            except (ValueError, TypeError):
                return activity_reply(status="ERROR", error="InvalidArguments", action="CORRECT_READ_ARGUMENTS")
        elif not _valid_fields(arguments):
            return {"status": "DENIED", "error": "InvalidBrokerArguments"}
        # A repeated frame is never a second backend call, even across pipe workers.
        # The backend runs outside this lock, because it can wait on an Owner window.
        with self._seen_lock:
            if call_id in self._seen_calls:
                return {"status": "DENIED", "error": "DuplicateBrokerCall"}
            if len(self._seen_calls) >= _MAX_SEEN_CALLS:
                return {"status": "DENIED", "error": "BrokerCallGuardFull"}
            self._seen_calls.add(call_id)
        try:
            result = getattr(self._backend, action)(**arguments)
            if action == "activity":
                from .activity import validate_reply, reply as activity_reply
                try:
                    return validate_reply(result, arguments["mode"], arguments.get("id"))
                except Exception:
                    return activity_reply(arguments["mode"], arguments.get("id"),
                                          status="ERROR", error="InvalidMetadata")
        except Exception:
            # No exception text: it may hold paths or command content.
            return self._unknown(arguments, "BrokerBackendFailed")
        # The existing Host lists sessions with no status field. Only these two
        # read-only listings may gain one; others must state their own outcome.
        if (action in _LISTINGS and type(result) is dict and "status" not in result
                and type(result.get("sessions")) is list):
            return {"status": "OK", **result}
        if type(result) is not dict or type(result.get("status")) is not str:
            return self._unknown(arguments, "BrokerBackendReplyInvalid")
        if result["status"] == "TIMED_OUT":
            if (not valid_no_answer(result)
                    or any(key in arguments and key in result and result[key] != arguments[key]
                           for key in ("pending_id", "request_id"))):
                return self._unknown(arguments, "BrokerBackendReplyInvalid")
        return result

    @staticmethod
    def _unknown(arguments, error):
        result = {"status": "OUTCOME_UNKNOWN", "error": error, "auto_retry": False}
        if "request_id" in arguments:
            result["request_id"] = arguments["request_id"]
        return result

    def _reply(self, action, call_id, result):
        try:
            return encode_frame({"v": 1, "action": action, "call_id": call_id,
                                 "result": result}, max_bytes=MAX_REPLY_BYTES)
        except (ValueError, TypeError):
            # The call may already have had an effect, so this is not a denial.
            unknown = {"status": "OUTCOME_UNKNOWN", "error": "BrokerReplyTooLarge",
                       "auto_retry": False}
            if type(result) is dict and type(result.get("request_id")) is str:
                unknown["request_id"] = result["request_id"]
            return encode_frame({"v": 1, "action": action, "call_id": call_id,
                                 "result": unknown})
