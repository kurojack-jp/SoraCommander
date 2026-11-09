"""B04 DEV_ONLY ncalrpc binding policy checklist. No RPC/OS authentication.

Never pass results to service admission, Broker execution or actual binding APIs.
"""
from dataclasses import dataclass
import re

# Constants cross-checked against Windows SDK rpcdce.h 10.0.26100.0.
WINNT = 10
PKT_PRIVACY = 6
QOS_V5 = 5
IDENTIFY = 2
STATIC = 0
MUTUAL_AUTH_REQUEST = 0x1
LOCAL_MA_HINT = 0x10
_ALLOWED_CAPS = MUTUAL_AUTH_REQUEST | LOCAL_MA_HINT


@dataclass(frozen=True, slots=True)
class BindingIntent:
    protocol: str = "ncalrpc"
    explicit_security: bool = True
    security_version: int = 1
    authn_service: int = WINNT
    authn_level: int = PKT_PRIVACY
    qos_version: int = QOS_V5
    capabilities: int = MUTUAL_AUTH_REQUEST | LOCAL_MA_HINT
    identity_tracking: int = STATIC
    impersonation: int = IDENTIFY
    effective_only: int = 1
    server_sid: str | None = "S-1-5-80-1-2-3-4-5"
    server_principal: str | None = None
    server_security_descriptor: bool = True
    # Claims NEVER constitute independent Windows evidence.
    claimed_trusted_installer: bool = False
    claimed_server_identity_verified: bool = False


@dataclass(frozen=True, slots=True)
class OfflineBindingInspection:
    configuration_consistent: bool
    missing_or_rejected: tuple[str, ...]
    authentication: str = "NOT_AUTHENTICATED"
    admission: str = "UNAVAILABLE"
    acceptance: str = "NOT_OS_ACCEPTED"


def inspect_intent(candidate: BindingIntent) -> OfflineBindingInspection:
    if type(candidate) is not BindingIntent:
        raise TypeError("BindingIntent required")
    missing = []
    def reject_if(condition: bool, reason: str) -> None:
        if condition:
            missing.append(reason)

    reject_if(type(candidate.protocol) is not str or candidate.protocol != "ncalrpc",
              "NOT_EXPLICIT_NCALRPC")
    reject_if(candidate.explicit_security is not True, "NULL_OR_INVALID_BINDING_SECURITY")
    for name, correct in (("security_version", 1), ("authn_service", WINNT),
                          ("authn_level", PKT_PRIVACY), ("qos_version", QOS_V5),
                          ("identity_tracking", STATIC), ("impersonation", IDENTIFY),
                          ("effective_only", 1)):
        actual = getattr(candidate, name)
        reject_if(type(actual) is not int or actual != correct,
                  "INVALID_OR_UNREVIEWED_" + name.upper())

    cap = candidate.capabilities
    reject_if(type(cap) is not int or (cap & ~_ALLOWED_CAPS) != 0
              or (cap & MUTUAL_AUTH_REQUEST) == 0,
              "INVALID_OR_MISSING_MUTUAL_AUTH_REQUEST")
    sid = candidate.server_sid
    # NT SERVICE SIDs are S-1-5-80 with exactly five decimal DWORD RIDs.
    # This checks shape only, never maps a SID to a protected installed service.
    canonical_sid = type(sid) is str and re.fullmatch(
        r"S-1-5-80(?:-(?:0|[1-9][0-9]{0,9})){5}", sid) is not None
    if canonical_sid:
        canonical_sid = all(int(part) <= 0xFFFFFFFF for part in sid.split("-")[4:])
    reject_if(not canonical_sid, "MISSING_OR_NONCANONICAL_SERVICE_SID")
    reject_if(candidate.server_principal is not None,
              "SID_AND_PRINCIPAL_MUST_NOT_BE_COMBINED")
    reject_if(candidate.server_security_descriptor is not True,
              "NO_SERVER_SECURITY_DESCRIPTOR_FOR_MUTUAL_AUTH")
    # An attacker can set every value and every claimed_* field above.
    # Therefore even a perfectly configured structure is NOT peer proof.
    good = not missing
    missing.append("ACTUAL_WINNT_QOS_SID_BEHAVIOR_NOT_OS_ACCEPTED")
    missing.append("PROTECTED_INSTALLER_PIN_AND_PEER_PROCESS_NOT_VERIFIED")
    missing.append("OWNER_TOKEN_AND_CALL_LIFETIME_NOT_VERIFIED")
    return OfflineBindingInspection(good, tuple(missing))
