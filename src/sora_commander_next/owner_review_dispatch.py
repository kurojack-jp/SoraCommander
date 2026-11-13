"""Explicit local Owner review dispatch. No remote approval action or startup on import."""
from threading import Lock, Thread
from .windows_owner_controller import WindowsOwnerController, diagnostic_fields, review_diagnostics
from .owner_approval_broker import OwnerApprovalBroker
from .no_answer import valid_no_answer

class OwnerReviewDispatcher:
    def __init__(self, controller):
        if type(controller) is not WindowsOwnerController:
            raise TypeError("Windows Owner controller required")
        controller._verify()
        self._controller = controller
        self._broker = OwnerApprovalBroker(controller)
        self._lock = Lock()
        self._active = None
        self._active_pending_id = None
        self._last_result = None
        self._last_job = None
        self._results = {}  # bounded pending-ID keyed outcomes; never grants authority

    def offer_local(self, pending_id):
        """Schedule at most one local dialog. Pending ID never grants approval."""
        if type(pending_id) is not str:
            return False
        self._controller._verify()
        with self._lock:
            # An already-consumed Owner review is no longer in pending_local.
            # Preserve read-only result attachment before checking that list.
            if pending_id in self._results:
                # A completed in-process review cannot open another UI.
                # read-only review_status supplies its existing outcome.
                return True
            if self._active is not None and self._active.is_alive():
                # Same pending Owner review: one GUI, multiple observers.
                return self._active_pending_id == pending_id
            if not any(p.pending_id == pending_id
                       for p in self._controller.pending_local()):
                return False
            thread = Thread(target=self._review, args=(pending_id,), daemon=True,
                            name="SoraOwnerReview")
            self._active = thread
            self._active_pending_id = pending_id
            thread.start()
            return True

    def _review(self, pending_id):
        try:
            result = self._broker.review_once(pending_id)
            if (type(result) is not dict or type(result.get('status')) is not str
                    or result['status'] not in {'ERROR', 'DENIED', 'TIMED_OUT', 'RUNNING', 'OUTCOME_UNKNOWN',
                        'UNKNOWN', 'BUSY', 'DENY_DUPLICATE_UNKNOWN', 'DENY_DUPLICATE_EXPIRED',
                        'DENY_DUPLICATE_CONFLICT', 'DENY_REPLAY_GUARD_FULL'}):
                raise ValueError('invalid broker result')
            if result["status"] == "TIMED_OUT" and (
                    result.get("stage") not in ("FIRST_GUI", "SECOND_GUI")
                    or not valid_no_answer(result)):
                raise ValueError("unverified Owner review timeout")
        except Exception:
            result = {"status": "OUTCOME_UNKNOWN", "error": "OwnerConfirmationUnavailable",
                      **review_diagnostics('LAUNCH', 'LAUNCH_UNKNOWN', 'UNKNOWN', 'UNKNOWN')}
        with self._lock:
            self._last_result = {"pending_id": pending_id, "status": result.get("status", "ERROR"),
                                 **diagnostic_fields(result)}
            self._last_job = result.get("session_id") if result.get("status") == "RUNNING" else None
            self._results[pending_id] = {"status": result.get("status", "ERROR"),
                                         "session_id": self._last_job, **diagnostic_fields(result)}
            while len(self._results) > 128:
                self._results.pop(next(iter(self._results)))
        # Observation only: nothing here may change or fail the review result,
        # so the import and every attribute read stay inside the guard.
        # Copy correlation under the approval lock, never inside dispatcher lock.
        host = None
        try:
            from .activity import publish
            host = self._controller.host
            matches = [rid for rid, pid, _ in host.approval.activity_snapshot() if pid == pending_id]
            if len(matches) != 1:
                host._activity_store.invalidate()
            else:
                publish(host._activity_store, request_id=matches[0], pending_id=pending_id,
                        session_id=result.get("session_id"), state=result["status"],
                        review_state=result["status"], source="review")
        except Exception:
            try:
                host._activity_store.invalidate()
            except Exception:
                pass

    def activity_snapshot(self):
        """Safe scalar snapshot; no Thread.is_alive, reviewer or polling."""
        with self._lock:
            result = tuple((pid, value.get("status"), value.get("session_id"))
                           for pid, value in self._results.items())
            active = self._active_pending_id
        if active is not None and all(pid != active for pid, _, _ in result):
            result += ((active, "PENDING", None),)
        return result

    def review_status(self, pending_id):
        """Read-only review outcome; knowing an ID never approves a command."""
        if type(pending_id) is not str or len(pending_id) != 32:
            return {"status": "ERROR", "error": "InvalidPendingId"}
        with self._lock:
            completed = self._results.get(pending_id)
            if completed is not None:
                return dict(completed)
            if (self._active_pending_id == pending_id and self._active is not None
                    and self._active.is_alive()):
                return {"status": "PENDING"}
        return {"status": "UNKNOWN"}

    def job_local(self):
        self._controller._verify()
        with self._lock:
            return self._last_job

    def result_local(self):
        self._controller._verify()
        with self._lock:
            return None if self._last_result is None else dict(self._last_result)
