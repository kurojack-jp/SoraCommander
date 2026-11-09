"""Development-only Broker-side sequencing of Owner review and ledger data.

This is deliberately INERT. A RESERVED_DATA_ONLY row cannot authorize a
process, and this module never imports JobManager or exposes an MCP endpoint.
A separately installed, Owner-authenticated protected Broker must own the
real executable admission transaction after its independent acceptance.
"""
from dataclasses import dataclass
from threading import RLock

from .approval_v2_contract import operation_for_command
from .approval_v2_flow_offline import OfflineOwnerReviewFlow
from .approval_v2_ledger import OfflineApprovalLedger
from .approval_v2_policy import SurfaceDecision, SurfacePolicyV2
from .commands import Command


@dataclass(frozen=True, slots=True)
class BrokerPreparationDataOnly:
    """Opaque non-authority receipt; never pass to a command launcher."""
    status: str
    request_id: str | None = None
    dispatch_id: str | None = None


class OfflineBrokerPreparation:
    """One-instance coordination; durable uniqueness lives in SQLite.

    This RLock does NOT serialize two independent Broker instances. Their
    concurrent reviews may open two Owner dialogs for the same request before
    SQLite denies the second claim. A live implementation needs a SINGLE
    independently protected Broker-owned review queue. Never authorize a
    process from a RESERVED_DATA_ONLY or ALREADY_* data receipt.

    Owner SID and machine strings in this offline test are labels, not trusted
    identity. The reviewer may be mocked; positive results are NOT grants.
    """

    def __init__(self, *, policy, ledger, reviewer, owner_sid, machine_id):
        if type(policy) is not SurfacePolicyV2:
            raise TypeError("offline v2 policy required")
        if type(ledger) is not OfflineApprovalLedger:
            raise TypeError("offline v2 ledger required")
        self._policy = policy
        self._ledger = ledger
        self._owner_sid = owner_sid
        self._machine_id = machine_id
        self._flow = OfflineOwnerReviewFlow(
            ledger, reviewer, owner_sid=owner_sid, machine_id=machine_id,
            policy=policy)
        self._lock = RLock()

    def prepare_data_only(self, command, *, allow_trusted=False):
        """Inspect -> review only if needed -> reserve exactly once.

        This method NEVER returns RUNNING, APPROVED or executable authority.
        Same request_id retries return the existing inert reservation; they
        must not show another Owner dialog or reserve a second dispatch.
        """
        if type(command) is not Command or type(allow_trusted) is not bool:
            return BrokerPreparationDataOnly("DENY")
        with self._lock:
            try:
                decision = self._policy.classify(command)
                if decision == SurfaceDecision.DENY:
                    return BrokerPreparationDataOnly("DENY", command.request_id)
                if decision == SurfaceDecision.AUTO:
                    return BrokerPreparationDataOnly(
                        "AUTO_POLICY_DATA_ONLY", command.request_id)
                if decision != SurfaceDecision.REVIEW:
                    return BrokerPreparationDataOnly("DENY", command.request_id)
                op = operation_for_command(
                    command, owner_id=self._owner_sid,
                    machine_id=self._machine_id)
                limits = dict(
                    timeout_ms=round(command.timeout * 1000),
                    output_limit=command.output_limit,
                    request_id=command.request_id)
                original = self._ledger.reserve_data_only(op, **limits)
                if original.status != "REVIEW":
                    return BrokerPreparationDataOnly(
                        original.status, command.request_id, original.dispatch_id)
                reviewed = self._flow.review_and_record_data_only(
                    command, allow_trusted=allow_trusted)
                if reviewed.status not in (
                        "RECORDED_DATA_ONLY", "ALREADY_RECORDED_DATA_ONLY"):
                    return BrokerPreparationDataOnly("DENY", command.request_id)
                reserved = self._ledger.reserve_data_only(op, **limits)
                return BrokerPreparationDataOnly(
                    reserved.status, command.request_id, reserved.dispatch_id)
            except Exception:
                return BrokerPreparationDataOnly("DENY", command.request_id)

    def conclude_data_only(self, request_id, outcome):
        """Record known/unknown outcome only; never re-submit an old request."""
        return self._ledger.conclude_data_only(request_id, outcome)

    def revoke_data_only(self, grant_id):
        """Offline owner-labeled revocation, not remotely callable."""
        return self._ledger.revoke_data_only(grant_id, self._owner_sid)
