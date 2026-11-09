"""B04 DEV_ONLY state-machine fixture: no Win32 API, actual identity or admission.

OfflineCall factory restricts accidental stage-forgery in tests, NOT a Python
security boundary. Never use this module in production authorization.
"""
from dataclasses import dataclass

_SEAL = object()


def _u32(x) -> bool:
    return type(x) is int and 0 <= x <= 0xFFFFFFFF


def _luid(v) -> bool:
    return (type(v) is tuple and len(v) == 2
            and all(_u32(part) for part in v))


@dataclass(frozen=True, slots=True)
class FrozenExpectation:
    protocol: str = "ncalrpc"
    authn_service: int = 10
    authn_level: int = 6
    interface: str = "dev-b04-synthetic-interface"
    opnum: int = 0
    call_id: str = "test-call-001"
    owner_sid: str = "S-1-5-21-42"
    logon_luid: tuple[int, int] = (1, 2)
    session_id: int = 3
    generation: str = "trusted-test-generation"
    approval_digest: str = "trusted-test-approval"
    epoch: str = "trusted-test-epoch"

    def __post_init__(self):
        # Fixed feasibility contract: caller must not redefine the security floor
        # by changing BOTH the expected data and the claimed call attributes.
        if (self.protocol != "ncalrpc" or self.authn_service != 10
                or self.authn_level != 6
                or self.interface != "dev-b04-synthetic-interface"
                or self.opnum != 0):
            raise ValueError("UNREVIEWED_RPC_SECURITY_OR_METHOD")
        for attr in ("protocol", "interface", "call_id", "owner_sid",
                     "generation", "approval_digest", "epoch"):
            v = getattr(self, attr)
            if type(v) is not str or not v or len(v) > 128:
                raise ValueError("INVALID_EXPECTED_" + attr.upper())
        if (not _u32(self.authn_service) or not _u32(self.authn_level)
                or not _u32(self.opnum) or not _u32(self.session_id)
                or not _luid(self.logon_luid)):
            raise ValueError("INVALID_EXPECTATION_SCHEMA")


@dataclass(frozen=True, slots=True)
class ClaimedAttributes:
    protocol: str = "ncalrpc"
    authn_service: int = 10
    authn_level: int = 6
    interface: str = "dev-b04-synthetic-interface"
    opnum: int = 0
    call_id: str = "test-call-001"
    null_session: bool = False
    call_status: str = "IN_PROGRESS"  # synthetic label, NOT RPC bitmask
    generation: str = "trusted-test-generation"
    approval_digest: str = "trusted-test-approval"
    epoch: str = "trusted-test-epoch"
    claimed_authenticated: bool = False
    claimed_pid: int = 1234


@dataclass(frozen=True, slots=True)
class ClaimedToken:
    user_sid: str = "S-1-5-21-42"
    logon_luid: tuple[int, int] = (1, 2)
    session_id: int = 3
    impersonation_level: str = "SecurityIdentification"
    claimed_os_verified: bool = False


@dataclass(frozen=True, slots=True, init=False)
class OfflineCall:
    state: str
    reject_reason: str | None
    revert_pending: bool
    expected: FrozenExpectation | None
    call_id: str | None

    def __init__(self, *, _seal=None, state="NEW", reject_reason=None,
                 revert_pending=False, expected=None, call_id=None):
        if _seal is not _SEAL:
            raise TypeError("OfflineCall factory required, NOT OS authority")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reject_reason", reject_reason)
        object.__setattr__(self, "revert_pending", revert_pending)
        object.__setattr__(self, "expected", expected)
        object.__setattr__(self, "call_id", call_id)

    @property
    def authentication(self) -> str:
        return "NOT_AUTHENTICATED"

    @property
    def admission(self) -> str:
        return "UNAVAILABLE"

    @property
    def os_acceptance(self) -> str:
        return "NOT_OS_ACCEPTED"

    @property
    def product_side_effects(self) -> int:
        return 0

    @property
    def must_revert(self) -> bool:
        return self.revert_pending

    @property
    def structural_only(self) -> bool:
        return (self.state == "REVERTED" and not self.revert_pending
                and self.reject_reason is None)


def _make(state, *, from_call=None, reason=None, pending=False,
          expected=None, call_id=None) -> OfflineCall:
    if from_call is not None:
        expected, call_id = from_call.expected, from_call.call_id
    return OfflineCall(_seal=_SEAL, state=state, reject_reason=reason,
                       revert_pending=pending, expected=expected,
                       call_id=call_id)


def _strict_eq(actual, expected) -> bool:
    return type(actual) is type(expected) and actual == expected


def begin_call(expectation: FrozenExpectation, attrs: ClaimedAttributes) -> OfflineCall:
    if type(expectation) is not FrozenExpectation or type(attrs) is not ClaimedAttributes:
        return _make("DENIED", reason="INVALID_RECORD_TYPE")
    try:
        expectation.__post_init__()
    except (TypeError, ValueError, AttributeError):
        return _make("DENIED", reason="INVALID_EXPECTATION_SCHEMA")
    for name in ("protocol", "authn_service", "authn_level", "interface",
                 "opnum", "call_id", "generation", "approval_digest", "epoch"):
        if not _strict_eq(getattr(attrs, name), getattr(expectation, name)):
            return _make("DENIED", reason="MISMATCH_" + name.upper())
    if attrs.null_session is not False:
        return _make("DENIED", reason="NULL_SESSION")
    if type(attrs.call_status) is not str or attrs.call_status != "IN_PROGRESS":
        return _make("DENIED", reason="CALL_NOT_ACTIVE")
    return _make("ATTRS_OK", expected=expectation, call_id=expectation.call_id)


def impersonate(call: OfflineCall, *, result_ok: bool) -> OfflineCall:
    if type(call) is not OfflineCall:
        return _make("FAIL_STOP_REQUIRED", reason="UNKNOWN_IMPERSONATION_STATE",
                     pending=True)
    if call.state == "FAIL_STOP_REQUIRED":
        return call
    if call.revert_pending:
        return _make("REVERT_REQUIRED", from_call=call,
                     reason="IMPERSONATE_OUT_OF_SEQUENCE", pending=True)
    if call.state != "ATTRS_OK":
        return _make("DENIED", from_call=call,
                     reason="IMPERSONATE_OUT_OF_SEQUENCE")
    if result_ok is not True:
        return _make("DENIED", from_call=call, reason="IMPERSONATE_FAILED")
    return _make("IMPERSONATED", from_call=call, pending=True)


def inspect_token(call: OfflineCall, expectation: FrozenExpectation,
                  token: ClaimedToken) -> OfflineCall:
    if type(call) is not OfflineCall:
        return _make("FAIL_STOP_REQUIRED", reason="UNKNOWN_IMPERSONATION_STATE",
                     pending=True)
    if call.state == "FAIL_STOP_REQUIRED":
        return call
    if call.state != "IMPERSONATED":
        if call.revert_pending:
            return _make("REVERT_REQUIRED", from_call=call,
                         reason="TOKEN_OUT_OF_SEQUENCE", pending=True)
        return _make("DENIED", from_call=call, reason="TOKEN_OUT_OF_SEQUENCE")
    if type(expectation) is not FrozenExpectation or expectation is not call.expected:
        return _make("REVERT_REQUIRED", from_call=call,
                     reason="EXPECTED_OWNER_REGISTRATION_CHANGED", pending=True)
    try:
        expectation.__post_init__()
    except (ValueError, TypeError, AttributeError):
        return _make("REVERT_REQUIRED", from_call=call,
                     reason="INVALID_EXPECTATION_SCHEMA", pending=True)
    if type(token) is not ClaimedToken or not _luid(token.logon_luid):
        return _make("REVERT_REQUIRED", from_call=call,
                     reason="INVALID_TOKEN_RECORD", pending=True)
    for name, expected in (
        ("user_sid", expectation.owner_sid),
        ("logon_luid", expectation.logon_luid),
        ("session_id", expectation.session_id),
        ("impersonation_level", "SecurityIdentification"),
    ):
        if not _strict_eq(getattr(token, name), expected):
            return _make("REVERT_REQUIRED", from_call=call,
                         reason="MISMATCH_" + name.upper(), pending=True)
    return _make("TOKEN_CHECKED", from_call=call, pending=True)


def revert(call: OfflineCall, *, result_ok: bool,
           same_active_thread: bool) -> OfflineCall:
    if type(call) is not OfflineCall:
        return _make("FAIL_STOP_REQUIRED", reason="UNKNOWN_IMPERSONATION_STATE",
                     pending=True)
    if call.state == "FAIL_STOP_REQUIRED":
        return call  # absorption; cannot be reset by successful-looking input
    if not call.revert_pending:
        return _make("DENIED", from_call=call, reason="REVERT_OUT_OF_SEQUENCE")
    if result_ok is not True or same_active_thread is not True:
        return _make("FAIL_STOP_REQUIRED", from_call=call,
                     reason="RPC_REVERT_NOT_PROVEN", pending=True)
    if call.state == "TOKEN_CHECKED" and call.reject_reason is None:
        return _make("REVERTED", from_call=call, pending=False)
    return _make("DENIED", from_call=call,
                 reason=call.reject_reason or "TOKEN_NOT_CHECKED", pending=False)
