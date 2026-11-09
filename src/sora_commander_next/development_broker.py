"""DEV_ONLY integration of source review, scheduler and existing Job runtime.

All dependencies are explicit, locally injected fixtures. Never imported by
product composition. Test ledger records are not protected service COMMIT;
no service-identity flag, DATA_* receipt or wire approval can arm production.
"""
from hashlib import sha256
import math
import re
from threading import RLock

from .broker_contract import BoundRequest
from .broker_host import encode_frame, decode_frame
from .broker_runtime import BrokerRuntime
from .jobs import JobManager
from .commands import Command
from .mcp_adapter import _validate_arguments


class DevelopmentBroker:
    def __init__(self, *, source_workflow, owner, scheduler, manager_factory,
                 ledger, command_review, executable, cwd, clock):
        self.source = source_workflow
        self.owner, self.scheduler = owner, scheduler
        self.ledger, self.command_review = ledger, command_review
        self.clock = clock
        self._lock = RLock()
        self._scheduled = set()
        self._commands = {}
        # Exactly one manager, injected into the existing runtime. Connections
        # never construct a runtime/manager and never close this owner.
        # The fixture dialect is not protected COMMIT. Disallow accidentally
        # injecting the real OS-backed JobManager into this mock-only Broker.
        # This is a development misuse guard, not a hostile-caller proof.
        if manager_factory is JobManager:
            raise TypeError("native JobManager factory forbidden in DEV_ONLY Broker")
        self.manager = manager_factory()
        if (isinstance(self.manager, JobManager)
                or getattr(self.manager, "DEV_SIMULATION_ONLY", None) is not True):
            raise TypeError("DEV_ONLY Broker requires explicit synthetic manager")
        self.runtime = BrokerRuntime(expected_executable=executable, expected_cwd=cwd,
            owner_binding=owner.binding()[0], owner_guard=owner, clock=clock, manager=self.manager)

    def handle(self, raw):
        frame = decode_frame(raw)
        if (set(frame) != {"v", "action", "call_id", "arguments"}
                or type(frame["v"]) is not int or frame["v"] != 1
                or type(frame["action"]) is not str or type(frame["call_id"]) is not str
                or re.fullmatch("[0-9a-f]{32}", frame["call_id"]) is None):
            raise ValueError("invalid development request envelope")
        from .broker_host import validate_source_arguments
        action, args = frame["action"], frame["arguments"]
        if action in {"request_source_create", "request_source_patch", "source_review_status", "cancel_source_review"}:
            validate_source_arguments(action, args)
        else:
            _validate_arguments(action, args)
        if self.owner.verify() is not True:
            raise PermissionError("Owner unavailable")
        with self._lock:
            if action in {"request_source_create", "request_source_patch"}:
                result = self.source.submit(action, args)
                rid = args["request_id"]
                if result["status"] == "REVIEW" and rid not in self._scheduled:
                    if len(self._scheduled) >= 64:
                        self.source.cancel(rid)
                        result = {"status": "DENIED", "error": "SchedulerCapacity"}
                    else:
                        self._scheduled.add(rid)  # reserve before scheduling; no retry
                        try:
                            self.scheduler.submit(lambda: self.source.review_local(rid))
                        except Exception:
                            self.source.cancel(rid)
                            result = {"status": "OUTCOME_UNKNOWN", "auto_retry": False}
            elif action == "source_review_status":
                result = self.source.status(args["request_id"])
            elif action == "cancel_source_review":
                result = self.source.cancel(args["request_id"])
            elif action in {"run_command", "run_command_wait"}:
                result = self._submit_command(args)
            elif action in {"request_status", "review_status", "await_command_result"}:
                rid = args.get("request_id", args.get("pending_id"))
                record = self._commands.get(rid)
                result = ({"status": "UNKNOWN", "request_id": rid} if record is None
                          else dict(record["result"]))
                if result.get("status") == "RUNNING":
                    result = self.runtime.query_result_page(rid)
            elif action in {"read_process_output", "await_process_output", "stop_process", "stop_process_wait"}:
                rid = next((rid for rid, sid in self.runtime.sessions.items()
                            if sid == args["session_id"]), None)
                if rid is None:
                    result = {"status": "DENIED"}
                elif action.startswith("stop_process"):
                    result = {"status": "CANCELLING" if self.runtime.cancel(rid) else "OUTCOME_UNKNOWN"}
                else:
                    # Existing runtime supplies 4 KiB pages within the 8 KiB
                    # envelope. No new spool, unbounded output or resubmission.
                    result = self.runtime.query_result_page(rid, offset=args.get("offset", 0))
            else:
                result = {"status": "UNAVAILABLE", "error": "DevelopmentOperationNotIntegrated"}
        if self.owner.verify() is not True:
            raise PermissionError("Owner lost before reply")
        return encode_frame({"v": 1, "action": action, "call_id": frame["call_id"], "result": result})

    def _submit_command(self, args):
        payload = {k: v for k, v in args.items() if k not in {"background", "wait_seconds"}}
        command = Command(**payload)
        rid = command.request_id
        # Freeze independent canonical data, including complete Owner tuple.
        raw = encode_frame({"executable": command.executable, "argv": list(command.argv),
            "cwd": command.cwd, "timeout": command.timeout, "output_limit": command.output_limit,
            "request_id": rid, "owner": self.owner.binding()})
        if rid in self._commands:
            record = self._commands[rid]
            return (dict(record["result"]) if record["raw"] == raw else {"status": "DENIED"})
        if len(self._commands) >= 64:
            return {"status": "DENIED", "error": "CommandHistoryFull"}
        record = {"raw": raw, "result": {"status": "REVIEW", "request_id": rid, "pending_id": rid}}
        self._commands[rid] = record
        issued = self.clock()
        if type(issued) not in (int, float) or not math.isfinite(issued) or issued < 0:
            record["result"] = {"status": "OUTCOME_UNKNOWN", "auto_retry": False}
            return dict(record["result"])
        try:
            self.scheduler.submit(lambda: self._review_command(record, issued))
        except Exception:
            record["result"] = {"status": "OUTCOME_UNKNOWN", "auto_retry": False}
        return dict(record["result"])

    def _review_command(self, record, issued):
        with self._lock:
            if record["result"]["status"] != "REVIEW":
                return
            record["result"] = {"status": "OUTCOME_UNKNOWN", "auto_retry": False}
            try:
                frozen = decode_frame(record["raw"])
                binding = (frozen["owner"][0], tuple(frozen["owner"][1]), frozen["owner"][2])
                def valid():
                    now = self.clock()
                    return (type(now) in (int, float) and math.isfinite(now)
                            and issued <= now < issued + 45
                            and self.owner.verify() is True and self.owner.binding() == binding)
                if not valid():
                    return
                if (self.command_review(record["raw"]) is not True
                        or not valid()):
                    return
                # DEV fixture dialect only. DATA_RECORDED_ONCE, untrusted wire
                # COMMITTED_FRESH and every unknown/late result refuse start.
                outcome = self.ledger.record_development_once(record["raw"])
                if (outcome != "DEV_RECORDED_ONCE" or type(outcome) is not str
                        or not valid()):
                    return
                command = BoundRequest(request_id=frozen["request_id"], executable=frozen["executable"],
                    argv=tuple(frozen["argv"]), cwd=frozen["cwd"], task_sha256=sha256(record["raw"]).hexdigest(),
                    owner_binding=frozen["owner"][0], expires_at=issued + 45,
                    timeout=frozen["timeout"], output_limit=frozen["output_limit"])
                record["result"] = self.runtime.start_approved(command,
                    approved_fingerprint=command.fingerprint(), current_task_sha256=command.task_sha256)
            except Exception:
                pass  # Unknown remains terminal. Never reschedule a consume/start.


class DevelopmentConnection:
    """Memory transport fixture; disconnect never tears down the Broker."""
    def __init__(self, broker):
        self.broker = broker
        self.closed = False

    def request(self, raw):
        if self.closed:
            raise ConnectionError("development connection closed")
        return self.broker.handle(raw)

    def close(self):
        self.closed = True
