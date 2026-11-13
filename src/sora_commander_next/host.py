"""Transport-independent host. Owner identity/configuration is local, never tool input."""

import os
import math
from time import monotonic, sleep

from .approval import LocalApproval
from .commands import Command
from .interactive_hooks import (hooks_for as _interactive_hooks_for,
                                input_status as _interactive_input_status,
                                prepare_interactive as _prepare_interactive)
from .jobs import JobManager, LaunchNotStarted, LaunchOutcomeUnknown, UnknownSession
from .policy import Decision, Policy
from .worker import execute


class Host:
    def __init__(self, policy=None, owner="local-owner", approval=None, worker=execute, jobs=None):
        self.policy = policy if policy is not None else Policy()
        self.owner, self.approval, self.worker = owner, approval or LocalApproval(), worker
        self.jobs = jobs if jobs is not None else JobManager()
        self._owner_review_dispatcher = None
        self._dev_interactive_jobs = None
        self._codex_job_reader = None
        from .activity import ActivityStore
        self._activity_store = ActivityStore()
        self.jobs._activity_store = self._activity_store
        self.approval._activity_store = self._activity_store

    def activity(self, mode="recent", id=None, limit=10, **extra):
        from .activity import observe
        return observe(self, dict(mode=mode, id=id, limit=limit, **extra))

    def _manager_for_request(self, request):
        if type(request) is not Command:
            raise TypeError("validated Command required")
        manager = self._dev_interactive_jobs if request.interactive else self.jobs
        if request.interactive and (type(manager) is not JobManager
                or manager is self.jobs or manager.protected_stage
                or manager.journal_path is not None or manager.output_archive_path is not None):
            raise PermissionError("DEV nonpersistent manager required")
        other = self.jobs if request.interactive else self._dev_interactive_jobs
        if other is not None:
            with other._lock:
                if (request.request_id in other._dev_interactive_requests
                        or other.request_status_readonly(request.request_id).get("status") != "UNKNOWN"):
                    raise PermissionError("request belongs to another manager")
        return manager

    def _manager_for_session(self, session_id):
        if (type(session_id) is not str or len(session_id) != 48
                or any(c not in "0123456789abcdef" for c in session_id)):
            raise ValueError("invalid session identity")
        dev = self._dev_interactive_jobs
        if dev is None:
            return self.jobs
        with self.jobs._lock, dev._lock:
            normal_known = session_id in self.jobs._jobs
            dev_known = session_id in dev._jobs
            if normal_known and dev_known:
                raise PermissionError("ambiguous session identity")
            if dev_known:
                return dev
            if normal_known:
                return self.jobs
        raise UnknownSession("unknown or expired session")

    def _input_manager(self, pending_id):
        matches = [manager for manager in (self.jobs, self._dev_interactive_jobs)
                   if manager is not None and pending_id in manager._stdin_pending]
        if len(matches) > 1:
            raise PermissionError("ambiguous pending identity")
        return matches[0] if matches else None

    def close(self):
        try:
            self.jobs.close()
        finally:
            if self._dev_interactive_jobs is not None:
                self._dev_interactive_jobs.close()

    def attach_owner_review_local(self, dispatcher):
        from .owner_review_dispatch import OwnerReviewDispatcher
        if type(dispatcher) is not OwnerReviewDispatcher or dispatcher._controller.host is not self:
            raise PermissionError("local Owner review dispatcher required")
        dispatcher._controller._verify()
        self._owner_review_dispatcher = dispatcher

    def ping(self):
        return {"status": "OK", "scaffold": True, "remote_ready": False}

    def status(self):
        return {**self.ping(), "approval": "LOCAL_DEMO_ONLY", "sessions": "IN_MEMORY",
                "process_descendants": "WINDOWS_JOB_OBJECT" if os.name == "nt" else "UNSUPPORTED"}

    def _context(self):
        return (self.owner, self.policy.roots, self.policy.generation, self.policy.instance_id)

    def run_command(self, background=False, *, interactive=False, **payload):
        result = self._run_command_activity_impl(background, interactive=interactive, **payload)
        # Observation only: it must never replace or fail the command result.
        try:
            from .activity import publish, valid_id
            if valid_id(result.get("request_id"), 32):
                publish(self._activity_store, request_id=result.get("request_id"),
                        pending_id=result.get("pending_id"), session_id=result.get("session_id"),
                        state=result.get("status"), source="activity_only")
        except Exception:
            pass
        return result

    def _run_command_activity_impl(self, background=False, *, interactive=False, **payload):
        request = None
        try:
            if type(background) is not bool:
                raise ValueError("background must be a bool")
            request = Command(interactive=interactive, **payload)
            from .activity import publish
            publish(self._activity_store, request_id=request.request_id,
                    state="PENDING", source="activity_only")
            if not background and request.timeout > 30:
                return {"request_id": request.request_id, "status": "ERROR",
                        "error": "LongCommandRequiresBackground"}
            with self.policy.lock:
                decision = self.policy.classify(request, self.owner)
                manager = self._manager_for_request(request) if (
                    self._dev_interactive_jobs is not None) else self.jobs
                manager._activity_store = self._activity_store
                if request.interactive:
                    # Interactive launches only ever narrow an Owner REVIEW.
                    # Any other decision is a plain denial, never an ERROR
                    # from the DEV precondition checks; with no DEV hooks
                    # installed the request is denied, never imported.
                    if decision != Decision.REVIEW:
                        return {"request_id": request.request_id, "status": "DENY"}
                    if _interactive_hooks_for(self._dev_interactive_jobs) is None:
                        return {"request_id": request.request_id, "status": "DENY",
                                "error": "DevInteractiveUnavailable"}
                    _prepare_interactive(self, request, background, decision)
                if decision == Decision.DENY:
                    return {"request_id": request.request_id, "status": "DENY"}
                preflight = manager.blocked_unknown_command_readonly(request)
                if preflight == "GUARD_UNAVAILABLE":
                    return {"request_id": request.request_id,
                            "status": "DENY_REPLAY_GUARD_FULL",
                            "error": "ProtectedBrokerLedgerRequired"}
                if preflight is True:
                    return {"request_id": request.request_id,
                            "status": "DENY_DUPLICATE_UNKNOWN",
                            "error": "PossibleDuplicateLaunchOutcomeUnknown"}
                if decision == Decision.REVIEW:
                    pending = self.approval.create(request, self._context())
                    if self._owner_review_dispatcher is not None:
                        # Fixed vocabulary only, never exception text. A
                        # locked/disconnected Owner session raises
                        # PermissionError from the controller's verify; False
                        # means another review dialog is active or this one
                        # is no longer pending.
                        unavailable = None
                        try:
                            if not self._owner_review_dispatcher.offer_local(pending.pending_id):
                                unavailable = "REVIEW_BUSY"
                        except PermissionError:
                            unavailable = "OWNER_SESSION_UNAVAILABLE"
                        except Exception:
                            unavailable = "OWNER_REVIEW_FAILED"
                        if unavailable is not None:
                            # Cached request IDs can outlive their consumed
                            # pending record. Only a record withdrawn here was
                            # never consumed, so only it proves NOT_STARTED; a
                            # consumed or expired one keeps the generic error.
                            try:
                                self.approval.reject_local(pending.pending_id)
                            except KeyError:
                                raise PermissionError("Owner review unavailable") from None
                            return {"request_id": request.request_id, "status": "ERROR",
                                    "error": "OwnerReviewUnavailable", "reason": unavailable,
                                    "launch_outcome": "NOT_STARTED", "auto_retry": False}
                    return {"request_id": request.request_id, "status": "REVIEW",
                            "pending_id": pending.pending_id, "expires_at": pending.expires_at}
                if decision not in (Decision.AUTO, Decision.SCOPED):
                    raise ValueError("invalid policy decision")
                if self.worker is not execute:
                    # A custom worker is not a JobManager admission path.
                    # Reject BOTH sync/background: silently falling back to
                    # real JobManager execution is unsafe for mock workers.
                    return {"request_id": request.request_id,
                            "status": "DENY_UNPROTECTED_WORKER",
                            "error": "JobManagerRequired"}
                result = self.jobs.start(request)
                publish(self._activity_store, request_id=request.request_id,
                        session_id=result.get("session_id"), state=result.get("status"),
                        source="activity_only")
            if not background and "session_id" in result:
                result = self.jobs.wait(result["session_id"])
            return {**result, "decision": decision.value}
        except LaunchOutcomeUnknown as exc:
            return {"request_id": request.request_id if request else None,
                    "status": "OUTCOME_UNKNOWN", "error": "LaunchOutcomeUnknown",
                    "launch_phase": exc.phase}
        except LaunchNotStarted as exc:
            return {"request_id": request.request_id if request else None,
                    "status": "ERROR", "error": "LaunchNotStarted",
                    "launch_phase": exc.phase}
        except Exception as exc:
            # Do not echo arbitrary exception messages, command text or secrets.
            return {"request_id": request.request_id if request else None,
                    "status": "ERROR", "error": type(exc).__name__}

    @staticmethod
    def _validate_wait(wait_seconds):
        if (type(wait_seconds) not in (int, float)
                or not math.isfinite(wait_seconds)
                or not 0 <= wait_seconds <= 90):
            raise ValueError("invalid bounded wait")
        return float(wait_seconds)

    def _wait_existing_job(self, session_id, deadline, *, decision=None, pending_id=None):
        """Wait on an already-started job. No new admission or approval."""
        try:
            snapshot = self._manager_for_session(session_id).wait_for(
                session_id, max(0.0, deadline - monotonic()))
            if decision is not None:
                snapshot["decision"] = decision
            if pending_id is not None:
                snapshot["pending_id"] = pending_id
            return snapshot
        except Exception as exc:
            return {
                "status": "ERROR", "error": type(exc).__name__,
                **({"pending_id": pending_id} if pending_id else {}),
            }

    def run_command_wait(self, *, wait_seconds=50.0, interactive=False, **payload):
        """Start a standard request, await local Owner click and return output.

        The Owner approval remains exclusively inside the existing Windows
        controller. No remote client can grant permission by calling this.
        A bounded wait may return PENDING/RUNNING, which is resumable without
        executing the command a second time.
        """
        try:
            duration = self._validate_wait(wait_seconds)
        except ValueError:
            return {"status": "ERROR", "error": "InvalidWaitSeconds"}
        deadline = monotonic() + duration
        if interactive:
            payload["interactive"] = interactive
        elif type(interactive) is not bool:
            return {"status": "ERROR", "error": "InvalidInteractive"}
        result = self.run_command(background=True, **payload)
        status = result.get("status")
        if status == "REVIEW":
            if self._owner_review_dispatcher is None:
                # Default deny-only Host never waits for an unattached GUI.
                return result
            return self._await_review_until(
                result["pending_id"], deadline)
        if status == "RUNNING" and "session_id" in result:
            return self._wait_existing_job(
                result["session_id"], deadline,
                decision=result.get("decision"))
        return result

    def _await_review_until(self, pending_id, deadline):
        """Observe Owner decision, then the same job, within one wait budget."""
        while True:
            review = self.review_status(pending_id)
            state = review.get("status")
            if state != "PENDING":
                if state == "RUNNING" and "session_id" in review:
                    result = self._wait_existing_job(
                        review["session_id"], deadline, pending_id=pending_id,
                        decision="REVIEW")
                    from .approval import diagnostic_fields
                    return {**result, **diagnostic_fields(review)}
                return {"pending_id": pending_id, **review}
            remaining = deadline - monotonic()
            if remaining <= 0:
                return {"pending_id": pending_id, "status": "PENDING"}
            sleep(min(0.10, remaining))

    def await_command_result(self, pending_id, *, wait_seconds=50.0):
        """Resume an existing Owner review/result without re-running a command."""
        if type(pending_id) is not str or len(pending_id) != 32:
            return {"status": "ERROR", "error": "InvalidPendingId"}
        try:
            duration = self._validate_wait(wait_seconds)
        except ValueError:
            return {"status": "ERROR", "error": "InvalidWaitSeconds"}
        manager = self._input_manager(pending_id)
        if manager is not None:
            return _interactive_input_status(manager, pending_id, duration)
        if self._owner_review_dispatcher is None:
            return {"pending_id": pending_id, "status": "DENY"}
        return self._await_review_until(pending_id, monotonic() + duration)

    def review_status(self, pending_id: str):
        """Observe local Owner review outcome; never approve via MCP."""
        manager = self._input_manager(pending_id)
        if manager is not None:
            return _interactive_input_status(manager, pending_id, 0)
        if self._owner_review_dispatcher is None:
            return {"status": "DENY"}
        return self._owner_review_dispatcher.review_status(pending_id)

    def request_status(self, request_id: str):
        """Read a known request by caller-chosen correlation ID, never submit.

        This is the in-memory Owner pilot only, not durable Broker identity
        or proof of authorization. UNKNOWN may mean the original submission
        was lost, expired, or never received; it never permits auto-replay.
        """
        if (type(request_id) is not str or len(request_id) != 32
                or any(c not in "0123456789abcdef" for c in request_id)):
            return {"status": "ERROR", "error": "InvalidRequestId"}
        try:
            job = self.jobs.request_status_readonly(request_id)
            if self._dev_interactive_jobs is not None:
                dev_job = self._dev_interactive_jobs.request_status_readonly(request_id)
                if dev_job.get("status") != "UNKNOWN":
                    if job.get("status") != "UNKNOWN":
                        raise PermissionError("ambiguous request identity")
                    return dev_job
            if job.get("status") != "UNKNOWN":
                return job
            pending_id = self.approval.pending_id_for_request_local(request_id)
            if pending_id is not None:
                return {"request_id": request_id, "pending_id": pending_id,
                        **self.review_status(pending_id)}
            return {"request_id": request_id, "status": "UNKNOWN"}
        except Exception:
            return {"request_id": request_id, "status": "ERROR",
                    "error": "RequestStatusUnavailable"}

    def read_process_output(self, session_id: str, offset: int = 0):
        try:
            return self._manager_for_session(session_id).read(session_id, offset)
        except Exception as exc:
            return {"status": "ERROR", "error": type(exc).__name__}

    def await_process_output(self, session_id: str, *, wait_seconds=50.0):
        """Read the result of an EXISTING job after a bounded wait.

        No command dispatch, Owner approval, or implicit retry occurs.
        Return RUNNING with the same session_id when the wait expires.
        """
        try:
            duration = self._validate_wait(wait_seconds)
            return self._manager_for_session(session_id).wait_for(session_id, duration)
        except Exception as exc:
            return {"status": "ERROR", "error": type(exc).__name__}

    def stop_process_wait(self, session_id: str, *, wait_seconds=50.0):
        """Explicit cancellation and bounded observation of the SAME job."""
        try:
            duration = self._validate_wait(wait_seconds)
        except ValueError:
            return {"status": "ERROR", "error": "InvalidWaitSeconds"}
        started = self.stop_process(session_id)
        if started.get("status") == "ERROR":
            return started
        result = self.await_process_output(session_id, wait_seconds=duration)
        return {**result, "stop_request_status": started.get("status")}

    def list_sessions(self):
        try:
            result = self.jobs.list_sessions()
            if self._dev_interactive_jobs is not None:
                result["sessions"].extend(self._dev_interactive_jobs.list_sessions()["sessions"])
                result["count"] = len(result["sessions"])
            return result
        except Exception as exc:
            return {"status": "ERROR", "error": type(exc).__name__}

    def previous_sessions(self):
        try:
            return self.jobs.previous_sessions()
        except Exception as exc:
            return {"status": "ERROR", "error": type(exc).__name__}

    def query_codex_job(self, job_id):
        """DEV_ONLY CodexBell observation; never execution or acceptance."""
        unavailable = {"status": "UNAVAILABLE", "supervision_verified": False,
                       "task_acceptance_verified": False, "auto_retry": False,
                       "state_mutated": False}
        if type(self) is not Host:
            return {**unavailable, "error": "ProtectedBrokerNotAccepted"}
        from .codex_jobs import LocalCodexJobs, validate_job_id
        try:
            validate_job_id(job_id)
        except ValueError:
            return {**unavailable, "status": "ERROR", "error": "InvalidJobId"}
        try:
            if self._codex_job_reader is None:
                self._codex_job_reader = LocalCodexJobs()
            return self._codex_job_reader.query(job_id)
        except FileNotFoundError:
            return {**unavailable, "status": "UNKNOWN", "error": "RecordNotFound"}
        except PermissionError:
            return {**unavailable, "error": "OwnerOrReadAccessRequired"}
        except Exception:
            return {**unavailable, "error": "RecordUnavailableOrInvalid"}

    def recovery_overview(self):
        """Read-only progress; private archive linkage needs live Owner."""
        archive_allowed = False
        archive_denial = False
        if self.jobs.output_archive_path is not None:
            try:
                reviewer = self._owner_review_dispatcher
                if reviewer is None:
                    archive_denial = True
                else:
                    reviewer._controller._verify()
                    archive_allowed = True
            except (PermissionError, OSError):
                # Keep the ordinary session journal readable for diagnostics.
                # Do NOT expose even archive request-ID metadata on denial.
                archive_denial = True
            except Exception:
                return {"status": "ERROR", "error": "OwnerVerificationUnavailable"}
        try:
            result = self.jobs.recovery_overview(
                include_archive_correlation=archive_allowed)
            if self._dev_interactive_jobs is not None:
                dev = self._dev_interactive_jobs.recovery_overview()
                for row in dev["sessions"]:
                    # Keep the existing status/result guidance. Nonpersistent
                    # does not mean a completed session is still running.
                    row.update(recoverable=False, persistence="NONE_DEV_INTERACTIVE",
                               auto_retry=False,
                               guidance=(
                                   "Recorded completion; verify effects, then you may continue. "
                                   "NO automatic resend."
                                   if row["next_step"] == "VERIFY_EFFECTS_THEN_CONTINUE"
                                   else "Inspect manually; NO automatic resend."))
                result["sessions"].extend(dev["sessions"])
                result["count"] = len(result["sessions"])
                result["dev_interactive"] = {
                    "recoverable": False, "persistence": "NONE",
                    "after_host_restart": "IDS_UNAVAILABLE_NO_REPLAY", "auto_retry": False}
            result["archive_correlation"] = (
                "OWNER_AUTHORIZATION_REQUIRED" if archive_denial
                else "AVAILABLE" if archive_allowed else "NOT_ENABLED")
            return result
        except Exception:
            return {"status": "ERROR", "error": "RecoveryOverviewUnavailable"}

    def previous_output(self, session_id=None):
        """Read-only, opt-in Owner log recovery; no job or grant resurrection."""
        if (session_id is not None
                and (type(session_id) is not str or len(session_id) != 48
                     or any(c not in "0123456789abcdef" for c in session_id))):
            return {"status": "ERROR", "error": "InvalidSessionId"}
        try:
            # Archived stdout/stderr can include secrets. A request/session
            # ID is not permission to read them. Verify the pinned local
            # Windows Owner on EVERY archive access, including index reads.
            if self.jobs.output_archive_path is not None:
                reviewer = self._owner_review_dispatcher
                if reviewer is None:
                    return {"status": "DENY", "error": "OwnerAuthorizationRequired"}
                reviewer._controller._verify()
            result = self.jobs.previous_output(session_id)
            # Only an Owner-verified archive may expose complete command
            # arguments. Persist nothing for unrecognized/sensitive commands.
            from .safe_command_identity import display_safe_command
            def with_safe_command(row):
                if type(row) is not dict:
                    return row
                line = display_safe_command(row["safe_command"]) if (
                    "safe_command" in row) else None
                safe = row.get("safe_command") if line is not None else None
                # Never echo unvalidated command identity fields from the
                # loaded row, even if the persisted archive has another
                # validator. Rebuild both fields only from the allowlisted
                # identity, not from an untrusted or legacy command_line.
                visible = {key: value for key, value in row.items()
                           if key not in ("safe_command", "command_line")}
                return {
                    **visible, "command_identified": line is not None,
                    **({"safe_command": {
                        "executable": safe["executable"],
                        "argv": list(safe["argv"]),
                        "cwd": safe["cwd"],
                    }, "command_line": line} if safe is not None else {}),
                }
            if "sessions" in result:
                return {**result, "sessions": [
                    with_safe_command(row) for row in result["sessions"]]}
            result = with_safe_command(result)
            if "chunks" in result:
                import base64
                stdout, stderr = [], []
                for piece in result["chunks"]:
                    raw = base64.b64decode(piece["data_base64"], validate=True)
                    (stdout if piece["stream"] == "stdout" else stderr).append(raw)
                return {
                    **result,
                    "stdout": b"".join(stdout).decode("utf-8", errors="replace"),
                    "stderr": b"".join(stderr).decode("utf-8", errors="replace"),
                }
            return result
        except PermissionError:
            return {"status": "DENY", "error": "OwnerAuthorizationRequired"}
        except Exception:
            return {"status": "ERROR", "error": "PreviousOutputUnavailable"}

    def stop_process(self, session_id: str):
        try:
            return self._manager_for_session(session_id).stop(session_id)
        except Exception as exc:
            return {"status": "ERROR", "error": type(exc).__name__}
