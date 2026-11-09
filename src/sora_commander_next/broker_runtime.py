"""In-process broker runtime core. No listener, autostart, or detached process.

A future Owner-started broker may own this object independently of MCP stdio.
"""
from .broker_contract import BrokerAdmission, BrokerRequestDenied
from .broker_protocol import decode_result_page_request, encode_result_page_response
import base64
import binascii
import math
from threading import RLock
from .commands import Command
from .jobs import JobManager


def _valid_live_page(result, request_id, session_id, offset):
    """Validate the entire bounded snapshot before disclosing any data."""
    fields = {"request_id", "session_id", "status", "exit_code", "offset",
              "next_offset", "captured_bytes", "output_incomplete", "overflow",
              "end_of_capture", "stdout_base64", "stderr_base64"}
    if type(result) is not dict or set(result) != fields:
        return False
    if (type(result["request_id"]) is not str
            or result["request_id"] != request_id
            or type(result["session_id"]) is not str
            or result["session_id"] != session_id):
        return False
    status, code = result["status"], result["exit_code"]
    if type(status) is not str or status not in (
            "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
            "OUTPUT_LIMIT", "OUTPUT_INCOMPLETE", "ERROR", "OUTCOME_UNKNOWN"):
        return False
    if code is not None and (type(code) is not int
                             or not -(2**31) <= code <= 2**32 - 1):
        return False
    if ((status == "RUNNING" and code is not None)
            or (status == "COMPLETED" and code != 0)
            or (status == "FAILED" and (code is None or code == 0))):
        return False
    if any(type(result[key]) is not int for key in (
            "offset", "next_offset", "captured_bytes")):
        return False
    captured, next_offset = result["captured_bytes"], result["next_offset"]
    if (result["offset"] != offset or not offset <= captured <= 65536
            or next_offset != min(captured, offset + 4096)):
        return False
    if any(type(result[key]) is not bool for key in (
            "output_incomplete", "overflow", "end_of_capture")):
        return False
    if status == "COMPLETED" and (result["output_incomplete"] or result["overflow"]):
        return False
    if result["end_of_capture"] != (next_offset == captured):
        return False
    size = 0
    for key in ("stdout_base64", "stderr_base64"):
        encoded = result[key]
        # Check encoded size BEFORE decoding, including hostile manager output.
        if type(encoded) is not str or len(encoded) > 5464:
            return False
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return False
        if base64.b64encode(raw).decode("ascii") != encoded:
            return False
        size += len(raw)
        if size > 4096:
            return False
    return size == next_offset - offset


class BrokerRuntime:
    def __init__(self, *, expected_executable, expected_cwd, owner_binding,
                 owner_guard, clock, capacity=4, manager=None):
        self.guard = owner_guard
        self.admission = BrokerAdmission(expected_executable, expected_cwd,
                                         owner_binding, clock=clock, capacity=capacity)
        self.manager = manager if manager is not None else JobManager(max_concurrent=capacity)
        self.sessions = {}
        self._lock = RLock()

    def start_approved(self, request, *, approved_fingerprint, current_task_sha256):
        # This method is NOT remotely exposed. Only trusted local approval flow
        # may invoke it, after verifying the actual Owner identity and task digest.
        self.guard.verify()
        with self._lock:
            return self._start_locked(request, approved_fingerprint, current_task_sha256)

    def _refresh_terminal_slots_locked(self):
        """Reclaim completed slots only on exact retained OS-child evidence.

        No background polling or client request can refund spent consent.
        Ambiguous/missing manager results leave capacity held, fail-closed.
        """
        for rid in self.admission.active_ids_local():
            sid = self.sessions.get(rid)
            if sid is None:
                continue
            try:
                confirmed = self.manager.confirm_terminal_readonly(rid, sid)
            except Exception:
                confirmed = False
            if confirmed is True:
                self.admission.note_verified_terminal_local(rid)

    def _start_locked(self, request, approved_fingerprint, current_task_sha256):
        # A job whose OS child was actually reaped must not permanently
        # consume a concurrency slot. Replay protection remains permanent
        # within this process even after slot reuse.
        self._refresh_terminal_slots_locked()
        self.admission.admit(request, approved_fingerprint=approved_fingerprint,
                             current_task_sha256=current_task_sha256,
                             owner_verified=True)
        command = Command(request.executable, request.argv, request.cwd,
                          timeout=request.timeout, output_limit=request.output_limit,
                          request_id=request.request_id)
        # Admission is one-use, but Owner may lock/log off between the first
        # identity check and the OS spawn. Recheck AFTER consuming admission
        # and IMMEDIATELY BEFORE handing the command to JobManager. A
        # failed recheck never refunds an approval or attempts a fallback.
        self.guard.verify()
        try:
            result = self.manager.start(command)
        except Exception:
            # Request is intentionally consumed: ambiguous launch cannot replay.
            raise
        # Never trust an ambiguous manager launch acknowledgement. The
        # admission was consumed regardless: no fallback launch or retry.
        if (type(result) is not dict or result.get("status") != "RUNNING"
                or result.get("request_id") != request.request_id
                or type(result.get("session_id")) is not str
                or len(result["session_id"]) != 48
                or any(c not in "0123456789abcdef" for c in result["session_id"])
                or result.get("reused_session") is True):
            return {"status": "UNKNOWN", "request_id": request.request_id}
        self.sessions[request.request_id] = result["session_id"]
        # The child may already be running; keep the session correlation
        # for later Owner-verified inspection, but never acknowledge a
        # start to a caller after the Owner session has become unavailable.
        self.guard.verify()
        return {"status": "RUNNING", "request_id": request.request_id}

    def query(self, request_id):
        self.guard.verify()
        with self._lock:
            session = self.sessions.get(request_id)
        if session is None:
            return None
        try:
            result = self.manager.read(session)
        except Exception:
            result = None
        self.guard.verify()
        if (type(result) is not dict
                or result.get("request_id") != request_id
                or result.get("session_id") != session):
            # Manager output must not leak a different job simply because
            # its session ID was once in this Broker's local index.
            return {"request_id": request_id, "status": "OUTCOME_UNKNOWN"}
        return result

    def query_result(self, request_id, *, offset=0):
        """Read an admitted job's retained result without launching it.

        This is an Owner-guarded in-process API, NOT remotely exposed IPC.
        Never resolve an arbitrary session_id supplied by the caller.
        """
        self.guard.verify()
        if (type(request_id) is not str or len(request_id) != 32
                or any(c not in "0123456789abcdef" for c in request_id)
                or type(offset) is not int or not 0 <= offset <= 65536):
            raise ValueError("invalid result lookup")
        with self._lock:
            session = self.sessions.get(request_id)
        if session is None:
            return {"request_id": request_id, "status": "UNKNOWN"}
        try:
            result = self.manager.read(session, offset)
        except Exception:
            result = None
        # Recheck Owner even if the lookup failed or the session was pruned.
        self.guard.verify()
        if (type(result) is not dict
                or result.get("request_id") != request_id
                or result.get("session_id") != session):
            return {"request_id": request_id, "status": "OUTCOME_UNKNOWN"}
        # Return the bounded JobManager read snapshot unchanged; the future
        # authenticated transport must apply separate wire-size constraints.
        return result

    def query_result_page(self, request_id, *, offset=0):
        """Owner-guarded raw page of a Broker-owned, already admitted job.

        Trusted in-process API only, NOT an authenticated external endpoint.
        No caller-supplied session, approval, execution, cancellation or retry.
        """
        self.guard.verify()
        if (type(request_id) is not str or len(request_id) != 32
                or any(c not in "0123456789abcdef" for c in request_id)
                or type(offset) is not int or not 0 <= offset <= 65536):
            raise ValueError("invalid result page lookup")
        with self._lock:
            session = self.sessions.get(request_id)
            if session is None:
                response = {"request_id": request_id, "status": "UNKNOWN"}
            else:
                try:
                    result = self.manager.read_live_page(session, offset)
                    # Detach from the manager before validating/returning.
                    result = dict(result) if type(result) is dict else None
                except Exception:
                    result = None
                if (self.sessions.get(request_id) == session
                        and _valid_live_page(result, request_id, session, offset)):
                    response = result
                else:
                    response = {"request_id": request_id,
                                "status": "OUTCOME_UNKNOWN"}
            # Last check follows lookup AND validation, including failed reads.
            self.guard.verify()
            return response

    def read_result_page_wire_local(self, raw):
        """Trusted in-process v2 read only; no listener or client authentication.

        Uses this runtime's existing Owner-guarded result correlation. Status
        describes retained data and never grants execution/approval authority.
        """
        self.guard.verify()
        try:
            request = decode_result_page_request(raw)
            with self._lock:
                data = self.query_result_page(request["request_id"],
                                              offset=request["offset"])
                return encode_result_page_response(data)
        finally:
            # After serialization, including parse/query/manager/codec failures.
            # A lock must prevent ALL response bytes from reaching the caller.
            self.guard.verify()

    def wait_result(self, request_id, *, wait_seconds=0):
        """Bounded Owner-local wait for an already admitted request.

        This is in-process only, not an IPC endpoint. Never launches or
        retries a job, and never accepts a caller-provided session ID.
        """
        self.guard.verify()
        if (type(request_id) is not str or len(request_id) != 32
                or any(c not in "0123456789abcdef" for c in request_id)
                or type(wait_seconds) not in (int, float)
                or not math.isfinite(wait_seconds)
                or not 0 <= wait_seconds <= 30):
            raise ValueError("invalid bounded result wait")
        with self._lock:
            session = self.sessions.get(request_id)
        if session is None:
            return {"request_id": request_id, "status": "UNKNOWN"}
        try:
            result = self.manager.wait_for(session, wait_seconds)
        except Exception:
            result = None
        # Recheck the Owner after every completed wait, including errors.
        # An Owner lock must take precedence over outcome/error disclosure.
        self.guard.verify()
        # A missing/mismatched job can never become a verified completion.
        if (type(result) is not dict
                or result.get("request_id") != request_id
                or result.get("session_id") != session):
            return {"request_id": request_id, "status": "OUTCOME_UNKNOWN"}
        return result

    def query_status(self, request_id):
        """Owner-checked, metadata-only status of this runtime's admitted job.

        Unlike query_result(), never reads stdout/stderr/chunks just to
        answer v1's status-only query. Request IDs are not authority.
        """
        self.guard.verify()
        if (type(request_id) is not str or len(request_id) != 32
                or any(c not in "0123456789abcdef" for c in request_id)):
            raise ValueError("invalid status lookup")
        with self._lock:
            session = self.sessions.get(request_id)
        if session is None:
            return {"request_id": request_id, "status": "UNKNOWN"}
        try:
            result = self.manager.request_status_readonly(request_id)
        except Exception:
            result = None
        # Status metadata is still Owner-scoped: no stale result after lock.
        self.guard.verify()
        if (type(result) is not dict
                or result.get("request_id") != request_id
                or result.get("session_id") != session):
            return {"request_id": request_id, "status": "OUTCOME_UNKNOWN"}
        # Strictly avoid forwarding result payloads into v1 query handling.
        status = result.get("status")
        code = result.get("exit_code")
        captured = result.get("captured_bytes")
        incomplete = result.get("output_incomplete")
        # Retained correlation alone cannot establish a completed result.
        # In particular bool == 0 and arbitrary strings must not become success.
        if (set(result) != {"request_id", "session_id", "status", "exit_code",
                            "captured_bytes", "output_incomplete"}
                or type(status) is not str or status not in (
                "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
                "OUTPUT_LIMIT", "OUTPUT_INCOMPLETE", "ERROR", "OUTCOME_UNKNOWN")
                or (code is not None and (type(code) is not int
                    or not -(2**31) <= code <= 2**32 - 1))
                or type(captured) is not int or not 0 <= captured <= 65536
                or type(incomplete) is not bool
                or (status == "RUNNING" and code is not None)
                or (status == "COMPLETED" and (type(code) is not int or code != 0 or incomplete))
                or (status == "FAILED" and (code is None or code == 0))):
            status = "OUTCOME_UNKNOWN"
        return {"request_id": request_id, "status": status}

    def cancel(self, request_id):
        """Cancel only this runtime's request-bound session, in-process only."""
        self.guard.verify()
        try:
            if (type(request_id) is not str or len(request_id) != 32
                    or any(c not in "0123456789abcdef" for c in request_id)):
                return False
            with self._lock:
                session = self.sessions.get(request_id)
                if session is None:
                    return False
                try:
                    result = self.manager.stop(session)
                except Exception:
                    return False
                return (type(result) is dict
                        and set(result) == {"session_id", "status"}
                        and type(result["session_id"]) is str
                        and result["session_id"] == session
                        and self.sessions.get(request_id) == session
                        and type(result["status"]) is str
                        and result["status"] in ("CANCELLING", "CANCELLED"))
        finally:
            # Stop may already have taken effect. Deny a stale Owner's ACK,
            # including on manager errors, without retrying or refunding consent.
            self.guard.verify()

    def make_local_dispatcher(self):
        """Bind v1 state queries/cancellation to THIS runtime's Owner guard.

        In-process only. This does not open IPC, authorize a client, or
        enable execution via the dispatcher. A real Windows pipe still needs
        independent authenticated-client checks and safe I/O acceptance.
        """
        from .broker_dispatch import BrokerDispatcher
        self.guard.verify()
        return BrokerDispatcher(
            owner_guard=self.guard,
            lookup=lambda request_id: self.query_status(request_id),
            cancel=lambda request_id: self.cancel(request_id),
        )

    def close(self):
        self.manager.close()
