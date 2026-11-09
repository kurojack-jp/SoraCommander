"""Owner-bound opt-in local controller: Windows logon checks before approval.

Not constructed by MCP adapters or HTTP; never exposed as an MCP tool.
This is a prerequisite to a later isolated GUI/IPC design, NOT a trusted
user-presence attestation or protection from malicious same-user Python.
"""
from .interactive_hooks import authorize_interactive_local as _authorize_interactive_local
from .interactive_hooks import hooks_for
from .approval import binding, review_diagnostics, diagnostic_fields
from dataclasses import dataclass
from contextlib import nullcontext
from time import monotonic
from .jobs import LaunchNotStarted
from .owner_ui import OwnerController
from .policy import Decision
from .windows_owner import OwnerSessionGuard
from .windows_session_events import OwnerSessionEventLatch


@dataclass(frozen=True)
class _ConsumedApproval:
    record: object
    policy: object
    context: tuple
    approval: object
    manager: object
    hooks: object
    owner_identity: object
    deadline: float
    guard: object
    session_latch: object
    normal_manager: object
    dev_manager: object
    ticket: object
    ticket_binding: object


def _ticket_binding(ticket):
    return tuple(ticket.get(key) for key in (
        'request', 'roots', 'identity', 'guard', 'expires', 'authorized', 'reviewing'))


class WindowsOwnerController(OwnerController):
    def __init__(self, host):
        super().__init__(host)
        # No caller-supplied guard or authorization token, including via MCP.
        self._guard = OwnerSessionGuard()
        self._verify()

    def _verify(self):
        self._guard.verify()
        if self.host.owner != self._guard.sid:
            raise PermissionError("Owner SID mismatch")

    def pending_local(self):
        try:
            self._verify()
            return super().pending_local()
        except (PermissionError, OSError):
            # No untrusted pending command text exposed on identity mismatch.
            return ()

    def approve_local(self, pending_id, *, session_latch=None):
        stage, state = 'OWNER_CHECK', 'NOT_CONSUMED'
        try:
            policy = self.host.policy
            with policy.lock:
                if session_latch is not None:
                    if type(session_latch) is not OwnerSessionEventLatch:
                        raise PermissionError("invalid local session latch")
                    session_latch.verify()
                self._verify()
                # Same lock is held from verification through single-use consume.
                stage = 'CONSUME'
                state = 'UNKNOWN'  # consume may remove a stale record then raise
                approval = self.host.approval
                context = self.host._context()
                record = approval.consume_local(pending_id, context)
                state = 'CONSUMED'
                if self.host.policy.classify(record.request, self.host.owner) != Decision.REVIEW:
                    raise PermissionError("review is no longer permitted")
                if self.host.approval.clock() >= record.expires_at:
                    raise PermissionError("review expired")
                # Keep session monitoring armed through the last validation.
                if session_latch is not None:
                    session_latch.verify()
                normal, dev = self.host.jobs, self.host._dev_interactive_jobs
                # Pin routing and existing authority before releasing any lock.
                # Lock order matches Host: policy -> normal -> DEV manager.
                with normal._lock, (dev._lock if dev is not None else nullcontext()):
                    manager = self.host._manager_for_request(record.request)
                    hooks = hooks_for(manager)
                    ticket = None
                    if record.request.interactive:
                        if session_latch is None or hooks is None:
                            raise PermissionError('interactive session history and hooks required')
                        ticket = manager._dev_interactive_requests.get(record.request.request_id)
                        if (ticket is None or ticket['request'] != record.request
                                or ticket.get('reviewing') or ticket['authorized']):
                            raise PermissionError('missing interactive review')
                    saved = _ConsumedApproval(
                        record, policy, context, approval,
                        manager, hooks, self._guard._identity,
                        monotonic() + (record.expires_at - approval.clock()),
                        self._guard, session_latch, normal, dev, ticket,
                        _ticket_binding(ticket) if ticket is not None else None)

            def verify_session():
                self._verify()  # pinned SID, LUID, WTS active and unlocked
                if saved.session_latch is not None:
                    saved.session_latch.verify()  # history, including lock then unlock
                if (self._guard is not saved.guard
                        or self._guard._identity != saved.owner_identity):
                    raise PermissionError('Owner identity changed')
                if ((record.request.interactive and monotonic() >= saved.deadline)
                        or saved.approval.clock() >= record.expires_at):
                    raise PermissionError('original approval expired')

            def verify_binding():
                verify_session()
                if (self.host.policy is not saved.policy
                        or self.host.approval is not saved.approval
                        or self.host._context() != saved.context
                        or binding(record.request, saved.context) != record.digest
                        or policy.classify(record.request, self.host.owner) != Decision.REVIEW
                        or self.host.jobs is not saved.normal_manager
                        or self.host._dev_interactive_jobs is not saved.dev_manager
                        or self.host._manager_for_request(record.request) is not saved.manager
                        or hooks_for(saved.manager) is not saved.hooks
                        or saved.manager._closed):
                    raise PermissionError('consumed approval changed')
                # classify/manager checks may take time or revoke stale grants.
                if (self.host.policy is not saved.policy
                        or self.host.approval is not saved.approval
                        or self.host._context() != saved.context):
                    raise PermissionError('policy changed during validation')
                verify_session()

            # Verify the original consumption before starting the optional
            # monitor. The monitor receives no authority and holds no locks.
            with policy.lock, saved.normal_manager._lock, (saved.dev_manager._lock
                    if saved.dev_manager is not None else nullcontext()):
                verify_binding()
            from .cli_monitor_bootstrap import ensure_monitor_for_approved_job
            stage = 'MONITOR'
            ensure_monitor_for_approved_job(record.request)
            with policy.lock, saved.normal_manager._lock, (saved.dev_manager._lock
                    if saved.dev_manager is not None else nullcontext()):
                verify_binding()
                if record.request.interactive:
                    from .dev_rdc_compat import _interactive_launch_shape
                    if (saved.manager._dev_interactive_requests.get(record.request.request_id)
                            is not saved.ticket
                            or _ticket_binding(saved.ticket) != saved.ticket_binding
                            or monotonic() >= saved.ticket['expires']
                            or _interactive_launch_shape(record.request, saved.ticket['roots'])
                            != saved.ticket['identity']):
                        raise PermissionError('interactive ticket changed during monitor')
                    # Shape validation can take time; keep the final authority
                    # and expiry check after it, before GUI2 preparation.
                    verify_binding()
                    if monotonic() >= saved.ticket['expires']:
                        raise PermissionError('interactive ticket expired during validation')
                else:
                    return self._launch(record, state)

            stage = 'SECOND_GUI'
            # Both policy.lock and manager._lock are free during the GUI.
            _authorize_interactive_local(self.host, record.request,
                                         deadline=min(monotonic() + 45, saved.deadline),
                                         verify=verify_session,
                                         before_launch=verify_binding)
            state, stage = 'SECOND_APPROVED', 'REVALIDATE'
            # Match Host session routing's normal -> DEV manager lock order.
            with policy.lock, saved.normal_manager._lock, saved.manager._lock:
                verify_binding()
                # GUI2 may legitimately set reviewing/authorized. The original
                # ticket identity and immutable fields must still match before
                # start() consumes the one-shot ticket.
                if (saved.manager._dev_interactive_requests.get(record.request.request_id)
                        is not saved.ticket
                        or _ticket_binding(saved.ticket)[:5] != saved.ticket_binding[:5]
                        or monotonic() >= saved.ticket['expires']):
                    raise PermissionError('interactive ticket changed during second GUI')
                return self._launch(record, state)
        except Exception:
            return {"status": "ERROR", "error": "OwnerAuthorizationRequired",
                    **review_diagnostics(stage, 'AUTHORIZATION_FAILED', state, 'NOT_STARTED')}

    def _launch(self, record, state):
        try:
            launch = self.host._manager_for_request(record.request).start(record.request)
        except LaunchNotStarted as exc:
            return {'request_id': record.request.request_id, 'status': 'ERROR',
                    'error': 'LaunchNotStarted',
                    **diagnostic_fields({'launch_phase': getattr(exc, 'phase', 'pre_spawn')}),
                    **review_diagnostics('LAUNCH', 'LAUNCH_NOT_STARTED', state, 'NOT_STARTED')}
        except Exception as exc:
            # Once start was entered, an unclassified failure is uncertain.
            # JobManager wraps OS launch faults; PermissionError is admission
            # only. Other unclassified failures never offer a retry.
            return {'request_id': record.request.request_id, 'status': 'OUTCOME_UNKNOWN',
                    'error': 'LaunchOutcomeUnknown',
                    **diagnostic_fields({'launch_phase': getattr(exc, 'phase', None)}),
                    **review_diagnostics('LAUNCH', 'LAUNCH_UNKNOWN', state, 'UNKNOWN')}
        if (type(launch) is not dict or type(launch.get('status')) is not str
                or launch['status'] not in {'RUNNING', 'BUSY', 'DENY_REPLAY_GUARD_FULL',
                    'DENY_DUPLICATE_CONFLICT', 'UNKNOWN', 'OUTCOME_UNKNOWN',
                    'DENY_DUPLICATE_UNKNOWN', 'DENY_DUPLICATE_EXPIRED'}):
            return {'status': 'OUTCOME_UNKNOWN', 'error': 'LaunchOutcomeUnknown',
                    **review_diagnostics('LAUNCH', 'LAUNCH_UNKNOWN', state, 'UNKNOWN')}
        unknown = launch['status'] in {'UNKNOWN', 'OUTCOME_UNKNOWN', 'DENY_DUPLICATE_UNKNOWN', 'DENY_DUPLICATE_EXPIRED'}
        outcome = 'UNKNOWN' if unknown else 'STARTED' if launch.get('status') == 'RUNNING' else 'NOT_STARTED'
        reason = 'LAUNCH_UNKNOWN' if unknown else 'LAUNCH_ACCEPTED' if outcome == 'STARTED' else 'LAUNCH_NOT_STARTED'
        return {**launch, 'decision': 'REVIEW',
                **review_diagnostics('LAUNCH', reason, state, outcome)}

    def reject_local(self, pending_id):
        try:
            with self.host.policy.lock:
                self._verify()
                self.host.approval.reject_local(pending_id)
                return {"status": "REJECTED"}
        except Exception:
            return {"status": "ERROR", "error": "OwnerAuthorizationRequired"}
