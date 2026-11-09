"""Phase9O: fail-closed protected trust-store INSTALLATION CHECKLIST.

Pure data / configuration review. NO ACL edits, key writes, files created,
elevation, local server launch, service or installer changes.
"""
from dataclasses import dataclass, field

class TrustBoundaryError(ValueError):
    pass

@dataclass(frozen=True)
class TrustStorePlan:
    code_root: str
    protected_policy_root: str
    writable_logs_root: str
    trusted_key_origin: str
    version_floor_origin: str
    approved: bool = field(default=False, init=False)

@dataclass(frozen=True)
class TrustStoreGate:
    status: str
    allowed_to_install: bool = field(default=False, init=False)
    allowed_to_activate: bool = field(default=False, init=False)
    allowed_to_connect_remote: bool = field(default=False, init=False)
    failures: tuple[str, ...] = ()

REQUIRED_EVIDENCE = (
    "publisher_key_pinned_out_of_band",
    "publisher_key_provenance_authenticated",
    "trusted_verifier_integrity",
    "code_owner_and_ancestor_acl_reviewed",
    "no_untrusted_code_modify",
    "no_untrusted_policy_modify",
    "protected_sequence_high_water",
    "durable_journal_recovery_test",
    "signed_dependencies_pinned",
    "update_file_swap_race_review",
    "owner_approval_gui_retest",
    "windows_lock_retest",
    "local_mcp_recovery_retest",
)

def _require_trust_origins(trusted_key_origin, version_floor_origin):
    """Reject self-attested or malformed trust origins, even on direct plans."""
    if (type(trusted_key_origin) is not str or
            trusted_key_origin not in ("OFFLINE_OWNER_VERIFIED", "SIGNED_ADMIN_INSTALLER")
            or type(version_floor_origin) is not str or
            version_floor_origin not in ("PROTECTED_ADMIN_JOURNAL", "EXTERNAL_SIGNED_LEDGER")):
        raise TrustBoundaryError("source of trust is not an approved design")


def _revalidate_stored_plan(plan):
    """Recheck direct dataclass constructions; never establish OS provenance."""
    from .release_layout import review_release_layout
    _require_trust_origins(plan.trusted_key_origin, plan.version_floor_origin)
    if plan.approved is not False:
        raise TrustBoundaryError("untrusted plan approval state")
    # The original development_root is not in TrustStorePlan; these two
    # permutations revalidate each stored Windows role and pairwise separation.
    # This is syntactic only: no Publisher, ACL, or Owner authentication.
    try:
        review_release_layout(development_root=plan.code_root,
                              code_root=plan.protected_policy_root,
                              data_root=plan.writable_logs_root)
        review_release_layout(development_root=plan.protected_policy_root,
                              code_root=plan.code_root,
                              data_root=plan.writable_logs_root)
    except ValueError as exc:
        raise TrustBoundaryError("stored trust layout is not a valid design") from exc


def plan_trust_store(*, development_root, code_root, protected_policy_root,
                     writable_logs_root, trusted_key_origin, version_floor_origin):
    from .release_layout import review_release_layout
    _require_trust_origins(trusted_key_origin, version_floor_origin)
    # The local staging/development root must be supplied explicitly, not
    # silently taken from the original developer's PC. This remains a
    # non-authorizing syntactic review: callers must independently establish
    # the actual source/staging identity before any real installation.
    try:
        review_release_layout(development_root=development_root,
                              code_root=code_root, data_root=writable_logs_root)
        review_release_layout(development_root=protected_policy_root,
                              code_root=code_root, data_root=writable_logs_root)
        review_release_layout(development_root=development_root,
                              code_root=protected_policy_root,
                              data_root=writable_logs_root)
    except ValueError as exc:
        raise TrustBoundaryError("trusted code, policy and logs must be distinct") from exc
    return TrustStorePlan(code_root, protected_policy_root, writable_logs_root,
                          trusted_key_origin, version_floor_origin)

def evaluate_trust_evidence(plan, *, evidence):
    """Review supplied evidence, not an install grant or authenticated trust."""
    if type(plan) is not TrustStorePlan or type(evidence) is not dict:
        raise TrustBoundaryError("explicit plan and evidence required")
    # Dataclass construction bypasses plan_trust_store(); do not mistake a
    # directly supplied plan and all-True evidence for a reviewed design.
    _revalidate_stored_plan(plan)
    if set(evidence) != set(REQUIRED_EVIDENCE):
        raise TrustBoundaryError("missing or extra evidence keys")
    failures=tuple(k for k in REQUIRED_EVIDENCE if evidence[k] is not True)
    return TrustStoreGate(
        status="TRUST_DESIGN_REVIEW_ONLY_ALL_CHECKS_PRESENT" if not failures
               else "TRUST_DESIGN_REVIEW_ONLY_MISSING_EVIDENCE",
        failures=failures,
    )
