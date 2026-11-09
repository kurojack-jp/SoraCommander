"""DEV_ONLY retained BrokerRuntime + immutable data-receipt recovery index.

Default construction is disarmed. Only the explicit mock factory accepts a
synthetic manager; real JobManager (including subclasses) is forbidden. No
service/Owner startup, listener, automatic consume or Host fallback exists.
"""
from dataclasses import dataclass
from hashlib import sha256
from threading import RLock

from .broker_contract import BoundRequest
from .broker_host import BrokerUnavailable, decode_frame, encode_frame
from .broker_runtime import BrokerRuntime
from .jobs import JobManager
from .offline_b01_full_binding import (FrozenCommand, parse_command, _decode,
    MAX_SNAPSHOT, make_wire_request)
from .service_spent_ledger_candidate import SpentObservation


@dataclass(frozen=True, slots=True)
class RetainedBinding:
    command_bytes: bytes
    request_bytes: bytes
    request_sha256: str
    generation_number: int


class RetainedBrokerCandidate:
    def __init__(self):
        raise BrokerUnavailable("native Broker composition not accepted")

    @classmethod
    def _for_mock(cls, runtime, *, broker_instance_id):
        if (type(broker_instance_id) is not str or len(broker_instance_id) != 32
                or any(c not in "0123456789abcdef" for c in broker_instance_id)):
            raise ValueError("fixed mock Broker instance identity required")
        if (type(runtime) is not BrokerRuntime or isinstance(runtime.manager, JobManager)
                or getattr(runtime.manager, "DEV_SIMULATION_ONLY", None) is not True):
            raise TypeError("explicit synthetic manager required; native startup disarmed")
        obj = object.__new__(cls)
        obj._runtime, obj._manager = runtime, runtime.manager
        obj._broker_instance_id = broker_instance_id
        obj._records, obj._lock, obj._alive = {}, RLock(), True
        return obj

    def _require_mock_manager(self):
        if (self._runtime.manager is not self._manager or isinstance(self._manager, JobManager)
                or getattr(self._manager, "DEV_SIMULATION_ONLY", None) is not True):
            self._alive = False
            raise TypeError("manager replaced or native manager detected")

    def _submit_mock_once(self, command, receipt, *, operation_id):
        """Private test seam ONLY. No transport action calls this method.

        Frozen SQL observations are forgeable data, never production consent.
        The only possible start target here is an explicit synthetic manager.
        Existing BrokerRuntime admission retains request-level replay fencing.
        """
        if (type(command) is not FrozenCommand or parse_command(command.canonical_bytes) != command
                or type(receipt) is not SpentObservation
                or receipt.status != "DATA_COMMIT_OBSERVED"
                or type(receipt.generation_number) is not int or not 0 < receipt.generation_number < 2**63):
            raise ValueError("exact data-only commit observation required")
        raw = make_wire_request(command, operation_id=operation_id)
        if (receipt.request_bytes != raw or receipt.request_sha256 != sha256(raw).hexdigest()
                or receipt.authority != "NONE_DATA_ONLY" or receipt.admission != "UNAVAILABLE"):
            raise ValueError("immutable ledger receipt binding mismatch")
        data = _decode(command.canonical_bytes, MAX_SNAPSHOT)
        if data["broker_instance_id"] != self._broker_instance_id:
            raise ValueError("saved receipt belongs to another Broker instance")
        rid = data["request_id"]
        binding = RetainedBinding(command.canonical_bytes, raw,
            receipt.request_sha256, receipt.generation_number)
        with self._lock:
            self._require_mock_manager()
            if not self._alive or rid in self._records or len(self._records) >= 64:
                return {"status": "OUTCOME_UNKNOWN", "request_id": rid, "auto_retry": False}
            # Retain UNKNOWN before any callback; no failure refunds the attempt.
            self._records[rid] = binding
            request = BoundRequest(rid, data["executable"], tuple(data["argv"]), data["cwd"],
                data["task_sha256"], data["owner_sid"], data["expires_at_tick_ms"] / 1000,
                data["timeout_ms"] / 1000, data["output_limit"])
            try:
                return self._runtime.start_approved(request,
                    approved_fingerprint=request.fingerprint(), current_task_sha256=request.task_sha256)
            except Exception:
                return {"status": "OUTCOME_UNKNOWN", "request_id": rid, "auto_retry": False}

    def note_mock_broker_death(self):
        with self._lock:
            self._alive = False  # No restart/reconsume/recovery of a fresh grant.

    def handle(self, raw):
        frame = decode_frame(raw)
        if (set(frame) != {"v", "action", "call_id", "arguments"}
                or type(frame["v"]) is not int or frame["v"] != 1
                or type(frame["call_id"]) is not str or len(frame["call_id"]) != 32
                or any(c not in "0123456789abcdef" for c in frame["call_id"])
                or type(frame["arguments"]) is not dict):
            raise ValueError("invalid recovery envelope")
        action, args = frame["action"], frame["arguments"]
        with self._lock:
            self._require_mock_manager()
            self._runtime.guard.verify()
            if action == "request_status" and set(args) == {"request_id"}:
                rid = args["request_id"]
                if type(rid) is not str or len(rid) != 32 or any(c not in "0123456789abcdef" for c in rid):
                    raise ValueError("invalid recovery request")
                binding = self._records.get(rid)
                result = {"status": "UNKNOWN", "request_id": rid}
                if binding is not None and self._alive:
                    result = self._runtime.query_status(rid)
                    sid = self._runtime.sessions.get(rid)
                    if sid is not None:
                        result = {**result, "session_id": sid, "ledger_binding": binding.request_sha256}
                elif binding is not None:
                    result = {"status": "OUTCOME_UNKNOWN", "request_id": rid, "auto_retry": False}
            elif action == "read_process_output" and set(args) == {"session_id", "offset"}:
                sid, offset = args["session_id"], args["offset"]
                if (type(sid) is not str or len(sid) != 48 or any(c not in "0123456789abcdef" for c in sid)
                        or type(offset) is not int or not 0 <= offset <= 65536):
                    raise ValueError("invalid recovery page")
                rid = next((rid for rid in self._records if self._runtime.sessions.get(rid) == sid), None)
                if rid is None:
                    result = {"status": "DENIED"}
                elif not self._alive:
                    result = {"status": "OUTCOME_UNKNOWN", "request_id": rid, "auto_retry": False}
                else:
                    result = self._runtime.query_result_page(rid, offset=offset)
            else:
                result = {"status": "UNAVAILABLE", "error": "RecoveryOnlyNoExecution"}
            encoded = encode_frame({"v": 1, "action": action, "call_id": frame["call_id"], "result": result})
            self._runtime.guard.verify()
            return encoded


class MockRecoveryConnection:
    """Connection lifetime never owns/closes the retained runtime or manager."""
    def __init__(self, broker):
        if type(broker) is not RetainedBrokerCandidate:
            raise TypeError("exact mock Broker required")
        self._broker, self._closed = broker, False

    def request(self, raw):
        if self._closed:
            raise ConnectionError("mock recovery connection closed")
        return self._broker.handle(raw)

    def close(self):
        self._closed = True
