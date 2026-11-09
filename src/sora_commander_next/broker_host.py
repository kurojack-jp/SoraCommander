"""Transport-only Host candidate. No execution, approval UI or Broker ownership.

Dependency injection is for local composition tests, NOT peer authentication.
The product factory deliberately has no accepted transport yet.
"""
import json
import re
from secrets import token_hex

from .policy import Policy

MAX_WIRE_BYTES = 8192
MAX_REPLY_BYTES = 262144


class BrokerUnavailable(ConnectionError):
    """Known not sent; there is no local execution fallback."""


def encode_frame(value, *, max_bytes=MAX_WIRE_BYTES):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True, allow_nan=False).encode("ascii")
    if not 0 < len(raw) <= max_bytes:
        raise ValueError("Broker frame exceeds explicit small-request scope")
    return raw


def decode_frame(raw, *, max_bytes=MAX_WIRE_BYTES):
    if type(raw) is not bytes or not 0 < len(raw) <= max_bytes:
        raise ValueError("invalid Broker frame")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    value = json.loads(raw.decode("ascii"), object_pairs_hook=unique)
    if type(value) is not dict or encode_frame(value, max_bytes=max_bytes) != raw:
        raise ValueError("canonical object required")
    return value


class UnavailableTransport:
    def request(self, raw):
        raise BrokerUnavailable("protected Broker transport not accepted")

    def close(self):
        pass


class BrokerHost:
    """Owns only one connection. An injected transport conveys no OS trust.

    No jobs/worker/approval attributes or legacy Host inheritance. EOF closes
    this connection only. Failed/late/malformed replies are never retried.
    """
    _max_request_bytes = MAX_WIRE_BYTES

    def __init__(self, transport, *, read_roots=(), read_only_roots=()):
        self._transport = transport
        # Historical read_roots argument denotes the original full scope.
        self.policy = Policy(roots=read_roots, read_only_roots=read_only_roots)
        self._closed = False

    def ping(self):
        return {"status": "OK", "remote_ready": False, "host_role": "TRANSPORT_ONLY"}

    def status(self):
        return {**self.ping(), "approval": "BROKER_ONLY_NOT_ACCEPTED",
                "product_execution": "NOT_READY", "service_identity": "NOT_AUTHENTICATED",
                "profile": "NOT_ACCEPTED", "owner_binding": "NOT_ACCEPTED"}

    def _call(self, action, **arguments):
        if self._closed:
            return {"status": "UNAVAILABLE", "error": "ConnectionClosed"}
        call_id = token_hex(16)
        try:
            if action in {"request_source_create", "request_source_patch", "source_review_status", "cancel_source_review"}:
                validate_source_arguments(action, arguments)
            raw = encode_frame({"v": 1, "action": action, "call_id": call_id,
                                "arguments": arguments},
                               max_bytes=self._max_request_bytes)
        except (ValueError, TypeError, UnicodeError):
            return {"status": "DENIED", "error": "SmallFrameScopeExceededOrInvalid"}
        try:
            reply = decode_frame(self._transport.request(raw), max_bytes=MAX_REPLY_BYTES)
            if (set(reply) != {"v", "action", "call_id", "result"}
                    or type(reply["v"]) is not int or reply["v"] != 1
                    or reply["action"] != action or reply["call_id"] != call_id
                    or type(reply["result"]) is not dict
                    or type(reply["result"].get("status")) is not str
                    or reply["result"]["status"].startswith(("DATA_", "COMMITTED_"))):
                raise ValueError("unbound Broker reply")
            # The envelope binds this exchange, not the job inside its result.
            # A mismatched result must not replace the caller's recovery ID or
            # disclose another request's outcome. Refusals may omit the ID.
            if ("request_id" in arguments and "request_id" in reply["result"]
                    and (type(reply["result"]["request_id"]) is not str
                         or reply["result"]["request_id"] != arguments["request_id"])):
                raise ValueError("unbound result request")
            return reply["result"]
        except BrokerUnavailable:
            return {"status": "UNAVAILABLE", "error": "ProtectedBrokerUnavailable"}
        except Exception:
            return {"status": "OUTCOME_UNKNOWN", "error": "BrokerReplyUnconfirmed",
                    "auto_retry": False}

    def run_command(self, background=False, **payload):
        # Validation and all policy/admission are repeated in the Broker.
        if type(background) is not bool:
            return {"status": "DENIED", "error": "InvalidBackground"}
        payload.setdefault("request_id", token_hex(16))
        return {"request_id": payload["request_id"],
                **self._call("run_command", background=background, **payload)}

    def run_command_wait(self, *, wait_seconds=50.0, **payload):
        payload.setdefault("request_id", token_hex(16))
        return {"request_id": payload["request_id"],
                **self._call("run_command_wait", wait_seconds=wait_seconds, **payload)}

    def request_status(self, request_id):
        return self._call("request_status", request_id=request_id)

    def review_status(self, pending_id):
        return self._call("review_status", pending_id=pending_id)

    def await_command_result(self, pending_id, *, wait_seconds=50.0):
        return self._call("await_command_result", pending_id=pending_id, wait_seconds=wait_seconds)

    def read_process_output(self, session_id, offset=0):
        return self._call("read_process_output", session_id=session_id, offset=offset)

    def await_process_output(self, session_id, *, wait_seconds=50.0):
        return self._call("await_process_output", session_id=session_id, wait_seconds=wait_seconds)

    def stop_process(self, session_id):
        return self._call("stop_process", session_id=session_id)

    def stop_process_wait(self, session_id, *, wait_seconds=50.0):
        return self._call("stop_process_wait", session_id=session_id, wait_seconds=wait_seconds)

    def list_sessions(self):
        return self._call("list_sessions")

    def previous_sessions(self):
        return self._call("previous_sessions")

    def previous_output(self, session_id=None):
        return self._call("previous_output", **({"session_id": session_id} if session_id else {}))

    def request_source_create(self, path, content, expected_new_sha256, request_id):
        from .policy import require_install_write_path
        try:
            require_install_write_path(path)
        except (OSError, ValueError, TypeError):
            return {"status": "REJECTED", "outcome": "NOT_STARTED",
                    "error": "RunningInstallWriteDenied", "retry_allowed": False}
        return self._call("request_source_create", path=path, content=content,
                          expected_new_sha256=expected_new_sha256, request_id=request_id)

    def request_source_patch(self, path, old_text, new_text, expected_sha256,
                             expected_new_sha256, request_id):
        from .policy import require_install_write_path
        try:
            require_install_write_path(path)
        except (OSError, ValueError, TypeError):
            return {"status": "REJECTED", "outcome": "NOT_STARTED",
                    "error": "RunningInstallWriteDenied", "retry_allowed": False}
        return self._call("request_source_patch", path=path, old_text=old_text,
                          new_text=new_text, expected_sha256=expected_sha256,
                          expected_new_sha256=expected_new_sha256, request_id=request_id)

    def source_review_status(self, request_id):
        return self._call("source_review_status", request_id=request_id)

    def cancel_source_review(self, request_id):
        return self._call("cancel_source_review", request_id=request_id)

    def close(self):
        if not self._closed:
            self._closed = True
            self._transport.close()


def validate_source_arguments(action, arguments):
    """Strict connector schema before SDK coercion; no consent field exists."""
    schemas = {
        "request_source_create": {"path", "content", "expected_new_sha256", "request_id"},
        "request_source_patch": {"path", "old_text", "new_text", "expected_sha256",
                                 "expected_new_sha256", "request_id"},
        "source_review_status": {"request_id"}, "cancel_source_review": {"request_id"},
    }
    if type(arguments) is not dict or set(arguments) != schemas[action]:
        raise ValueError("invalid source request fields")
    if any(type(v) is not str for v in arguments.values()):
        raise ValueError("exact source string types required")
    for key, value in arguments.items():
        if key.endswith("sha256") and re.fullmatch("[0-9a-f]{64}", value) is None:
            raise ValueError("invalid source digest")
    if re.fullmatch("[0-9a-f]{32}", arguments["request_id"]) is None:
        raise ValueError("invalid source request ID")
    if "path" in arguments and (not arguments["path"] or "\0" in arguments["path"]):
        raise ValueError("invalid source path")
    for key in ("content", "old_text", "new_text"):
        if key in arguments and len(arguments[key].encode("utf-8")) > 16384:
            raise ValueError("source exceeds 16 KiB limit")
    if "old_text" in arguments and not arguments["old_text"]:
        raise ValueError("nonempty literal anchor required")
    encode_frame({"v": 1, "action": action, "call_id": "0" * 32, "arguments": arguments})
