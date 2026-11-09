"""Offline v2 Owner review -> SQLite claim flow, with NO execution authority.

This module does not authenticate the Owner, issue a usable OS grant, or
dispatch commands. The supplied reviewer is deliberately mockable for tests.
Never expose it over MCP, or treat its returned DATA_ONLY marker as permission.
Before runtime promotion the review, storage and launch must be one trusted
Broker-owned transaction with Windows identity, lock and IPC verification.
"""
from dataclasses import dataclass
from hashlib import sha256
from threading import RLock
from uuid import uuid4

from .approval_v2_contract import ConsentRecord, operation_for_command
from .approval_v2_ledger import LedgerOutcome, OfflineApprovalLedger
from .approval_v2_owner_local import LocalReviewData
from .commands import Command
from .approval_v2_policy import SurfaceDecision, SurfacePolicyV2


def evaluate_request_data_only(policy, ledger, command, *, owner_sid,
                               machine_id, reserve=False):
    """New v2-only surface policy before ledger lookup, NEVER execute.

    The policy has no legacy SCOPED grants or mutable authorization state.
    This is an inert preview; the eventual Owner broker must re-check its
    real OS Owner/session state under the live dispatch lock.
    """
    if (type(policy) is not SurfacePolicyV2
            or type(ledger) is not OfflineApprovalLedger
            or type(command) is not Command or type(reserve) is not bool):
        return LedgerOutcome("DENY")
    try:
        decision = policy.classify(command)
        if decision == SurfaceDecision.DENY:
            return LedgerOutcome("DENY")
        if decision == SurfaceDecision.AUTO:
            return LedgerOutcome("AUTO_POLICY_DATA_ONLY")
        if decision != SurfaceDecision.REVIEW:
            return LedgerOutcome("DENY")
        op = operation_for_command(
            command, owner_id=owner_sid, machine_id=machine_id)
        milliseconds = round(command.timeout * 1000)
        if reserve:
            return ledger.reserve_data_only(
                op, timeout_ms=milliseconds, output_limit=command.output_limit,
                request_id=command.request_id)
        return ledger.inspect(op, timeout_ms=milliseconds,
                              output_limit=command.output_limit)
    except Exception:
        return LedgerOutcome("DENY")


@dataclass(frozen=True, slots=True)
class OfflineReviewOutcome:
    status: str
    grant_id: str | None = None


class OfflineOwnerReviewFlow:
    """Serializes explicit review data and ledger import, with zero execution."""

    def __init__(self, ledger, reviewer, *, owner_sid, machine_id, policy):
        if type(ledger) is not OfflineApprovalLedger:
            raise TypeError("offline ledger required")
        if type(policy) is not SurfacePolicyV2:
            raise TypeError("v2 surface policy required")
        if not callable(getattr(reviewer, "review_once_local", None)):
            raise TypeError("local reviewer interface required")
        # Offline context only: these strings are NOT proof of OS identity.
        for value in (owner_sid, machine_id):
            if (type(value) is not str or not 1 <= len(value) <= 256
                    or "\0" in value):
                raise ValueError("bounded offline Owner/machine label required")
        self._ledger = ledger
        self._policy = policy
        self._reviewer = reviewer
        self._owner_sid = owner_sid
        self._machine_id = machine_id
        self._lock = RLock()

    def review_and_record_data_only(self, request, *, allow_trusted=False):
        if type(request) is not Command or type(allow_trusted) is not bool:
            return OfflineReviewOutcome("DENY")
        with self._lock:
            # Static DENY must happen before any Owner prompt or recording.
            # Safe fixed AUTO never creates a reusable Owner grant.
            decision = self._policy.classify(request)
            if decision == SurfaceDecision.DENY:
                return OfflineReviewOutcome("DENY")
            if decision == SurfaceDecision.AUTO:
                return OfflineReviewOutcome("AUTO_POLICY_DATA_ONLY")
            if decision != SurfaceDecision.REVIEW:
                return OfflineReviewOutcome("DENY")
            expected = operation_for_command(
                request, owner_id=self._owner_sid, machine_id=self._machine_id)
            # A matching active claim skips the Owner dialog entirely.
            # This is deliberately only a data lookup, not an execution grant;
            # the live Broker still needs its own authenticated admission.
            try:
                existing = self._ledger.inspect(
                    expected, timeout_ms=round(request.timeout * 1000),
                    output_limit=request.output_limit)
            except Exception:
                return OfflineReviewOutcome("DENY")
            if existing.status == "MATCHING_DATA_ONLY":
                return OfflineReviewOutcome(
                    "ALREADY_RECORDED_DATA_ONLY", existing.grant_id)
            if existing.status != "REVIEW":
                return OfflineReviewOutcome("DENY")
            try:
                review = self._reviewer.review_once_local(
                    request, allow_trusted=allow_trusted)
            except Exception:
                return OfflineReviewOutcome("DENY")
            if type(review) is not LocalReviewData:
                return OfflineReviewOutcome("DENY")
            denied = review.status == "DENIED_DATA_ONLY"
            if (review.status not in ("LOCAL_CHOICE_DATA_ONLY", "DENIED_DATA_ONLY")
                    or (denied and review.choice is not None)
                    or (not denied and review.choice not in ("ONCE", "TRUSTED"))
                    or (review.choice == "TRUSTED" and not allow_trusted)
                    or review.owner_sid != self._owner_sid
                    or review.spec_sha256 != expected.digest
                    or review.request_id != request.request_id
                    or type(review.review_id) is not str
                    or not 1 <= len(review.review_id) <= 128
                    or type(review.review_sha256) is not str
                    or len(review.review_sha256) != 64):
                return OfflineReviewOutcome("DENY")
            # Data origin is untrusted: review.sha is checked by ConsentRecord.
            # Include the review ID and exact displayed scope to prevent a
            # second local claim from reusing a challenge in this pilot.
            evidence_bytes = (
                "SoraCommanderNext/v2/offline-review/v1\0"
                + review.review_id + "\0" + review.review_sha256 + "\0"
                + expected.digest + "\0" + self._owner_sid
            ).encode("utf-8")
            claim = ConsentRecord(
                approval_id=uuid4().hex + uuid4().hex,
                spec_sha256=expected.digest,
                owner_id=self._owner_sid,
                choice="DENY" if denied else review.choice,
                review_sha256=review.review_sha256,
                owner_evidence_sha256=sha256(evidence_bytes).hexdigest(),
                revision=1,
                max_timeout_ms=round(request.timeout * 1000),
                max_output_limit=request.output_limit,
            )
            try:
                outcome = self._ledger.record_claim(expected, claim)
            except Exception:
                return OfflineReviewOutcome("DENY")
            if denied:
                # The rejection consumes review evidence but creates no grant.
                return OfflineReviewOutcome("DENY")
            if outcome.status != "CLAIM_RECORDED_DATA_ONLY":
                return OfflineReviewOutcome("DENY")
            return OfflineReviewOutcome("RECORDED_DATA_ONLY", outcome.grant_id)
