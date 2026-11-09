"""Read-only, transport-independent broker dispatcher (no IPC listener or spawn).

Authentication is a prerequisite supplied by a trusted transport adapter.
"""
from .broker_protocol import decode_request, encode_response, ProtocolRejected


class BrokerDispatcher:
    def __init__(self, *, owner_guard, lookup, cancel):
        self._guard = owner_guard
        self._lookup = lookup
        self._cancel = cancel

    def handle(self, raw):
        try:
            request = decode_request(raw)
        except ProtocolRejected:
            return encode_response({"version": 1, "status": "DENIED", "request_id": None})
        rid = request.get("request_id")
        try:
            self._guard.verify()
        except Exception:
            return encode_response({"version": 1, "status": "DENIED", "request_id": rid})
        op = request["op"]
        if op == "health":
            status = "OK"
        elif op == "query":
            try:
                # A non-None result is not proof that a job exists:
                # backend {"status":"UNKNOWN"} must NEVER become OK.
                result = self._lookup(rid)
                if type(result) is not dict:
                    status = "UNKNOWN"
                elif result.get("status") in (
                        "UNKNOWN", "OUTCOME_UNKNOWN", "INTERRUPTED_UNKNOWN",
                        "DENY", "DENIED", "ERROR"):
                    status = "UNKNOWN"
                elif result.get("status") in (
                        "RUNNING", "COMPLETED", "FAILED", "CANCELLED",
                        "CANCELLING", "TIMEOUT", "OUTPUT_LIMIT",
                        "OUTPUT_INCOMPLETE"):
                    status = "OK"
                else:
                    status = "UNKNOWN"
            except Exception:
                status = "UNKNOWN"
        else:
            try:
                # Cancellation is scoped by the backend to a live, owned job.
                status = "OK" if self._cancel(rid) is True else "UNKNOWN"
            except Exception:
                status = "UNKNOWN"
        return encode_response({"version": 1, "status": status, "request_id": rid})
