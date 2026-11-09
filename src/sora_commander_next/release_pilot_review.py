"""Phase9P: read-only isolated pilot readiness checklist.

No installation, ACL edit, key generation, network listener or service.
The returned result can never authorize an action.
"""
from dataclasses import dataclass
from .release_layout import review_release_layout
from .release_trust_store_plan import plan_trust_store

CHECKS = (
    "independent_publisher_key",
    "trusted_verifier",
    "protected_release_journal",
    "admin_owned_code",
    "acl_ancestor_review",
    "python_runtime_and_dll_paths",
    "signed_manifest",
    "approved_icon_integrity",
    "separate_mutable_data",
    "atomic_update_and_recovery",
    "owner_approval_and_lock",
    "local_mcp_regression",
    "separate_owner_authorization",
)

@dataclass(frozen=True)
class PilotReview:
    missing: tuple[str, ...]
    status: str = "PREINSTALL_REVIEW_ONLY"
    can_install: bool = False
    can_enable_service: bool = False
    can_enable_remote: bool = False

def review_isolated_pilot(*, development_root, code_root, policy_root,
                          logs_root, reviewed):
    plan_trust_store(development_root=development_root,
                     code_root=code_root,protected_policy_root=policy_root,
                     writable_logs_root=logs_root,
                     trusted_key_origin="OFFLINE_OWNER_VERIFIED",
                     version_floor_origin="PROTECTED_ADMIN_JOURNAL")
    if type(reviewed) is not dict or set(reviewed) != set(CHECKS):
        raise ValueError("explicit complete evidence list required")
    return PilotReview(tuple(key for key in CHECKS if reviewed[key] is not True))
