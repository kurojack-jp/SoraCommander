"""DEV_ONLY typed B04 comparison. No SCM/pipe/token calls or authority.

Policy and configuration inputs are candidate records, not proof of protected
provenance. A matching result ALWAYS retains the missing native trust gates.
"""
from dataclasses import dataclass, field

from .offline_b04_service_identity_probe import SERVICE_NAME, ServiceProcessObservation
from .service_native_prerequisites import NativePrerequisites, MISSING_EVIDENCE, _path
from .offline_b01_full_binding import ContractError, _hex, _HEX32, _HEX64


def _text(value, limit=32768):
    if type(value) is not str or not 0 < len(value) < limit or "\0" in value:
        raise ContractError("invalid configuration text")


@dataclass(frozen=True, slots=True)
class ReleasePolicyCandidate:
    installation_id: str
    generation: str
    release_sequence: int
    image_path: str
    image_sha256: str
    service_sid: str

    def __post_init__(self):
        for value in (self.installation_id, self.generation):
            _hex(value, _HEX32, "release identity")
        _hex(self.image_sha256, _HEX64, "image digest")
        _path(self.image_path)
        if type(self.release_sequence) is not int or not 1 <= self.release_sequence < 2**63:
            raise ContractError("invalid release sequence")
        import re
        if (type(self.service_sid) is not str or re.fullmatch(
                r"S-1-5-80(?:-(?:0|[1-9][0-9]{0,9})){5}", self.service_sid) is None
                or any(int(n) > 0xffffffff for n in self.service_sid.split("-")[4:])):
            raise ContractError("exact per-service SID required")


@dataclass(frozen=True, slots=True)
class ServiceConfigurationRecord:
    """Decoded QueryServiceConfig/Config2 data; not a native observation API.

    Service SID type RESTRICTED is 3 (includes UNRESTRICTED bit), not 2.
    BinaryPathName is compared as a complete quoted command line; no parsing
    an unquoted executable, argument dropping, expansion or path normalization.
    """
    service_name: str
    binary_command_line: str
    account: str
    service_type: int
    start_type: int
    error_control: int
    sid_type: int

    def __post_init__(self):
        for value in (self.service_name, self.binary_command_line, self.account):
            _text(value)
        for value in (self.service_type, self.start_type, self.error_control, self.sid_type):
            if type(value) is not int or not 0 <= value <= 0xffffffff:
                raise ContractError("DWORD configuration required")


@dataclass(frozen=True, slots=True)
class AuthenticationPrerequisites:
    configuration_matches: bool
    mismatches: tuple
    policy_identity: tuple
    authentication: str = field(default="NOT_AUTHENTICATED", init=False)
    admission: str = field(default="UNAVAILABLE", init=False)
    missing_evidence: tuple = field(default=MISSING_EVIDENCE + (
        "SCM_CONFIG_SAMPLES_NOT_BOUND_TO_RETAINED_SERVICE_HANDLE",
        "POLICY_PROVENANCE_AND_IMAGE_DIGEST_NOT_OS_VERIFIED",
    ), init=False)


def compare_authentication_prerequisites(policy, initial, final, native):
    """Combine existing records, with no caller-supplied trust Boolean.

    Equal configuration samples cannot prove absence of intervening changes.
    Never remove missing evidence based on equal or allegedly signed inputs.
    """
    if (type(policy) is not ReleasePolicyCandidate
            or type(initial) is not ServiceConfigurationRecord
            or type(final) is not ServiceConfigurationRecord
            or type(native) is not NativePrerequisites
            or type(native.process) is not ServiceProcessObservation):
        raise ContractError("exact prerequisite records required")
    # Revalidate frozen records as well; their constructors are not a trust boundary.
    policy.__post_init__()
    initial.__post_init__()
    final.__post_init__()
    errors = []
    expected = (SERVICE_NAME, '"' + policy.image_path + '"',
                "NT AUTHORITY\\LocalService", 0x10, 3, 1, 3)
    values = lambda r: (r.service_name, r.binary_command_line, r.account,
                       r.service_type, r.start_type, r.error_control, r.sid_type)
    if values(initial) != expected:
        errors.append("INSTALLED_CONFIGURATION_MISMATCH")
    if final != initial:
        errors.append("CONFIGURATION_CHANGED")
    process = native.process
    if (process.service_name != SERVICE_NAME
            or type(process.pid) is not int or not 1 <= process.pid <= 0xffffffff
            or type(process.created_filetime) is not int or not 0 < process.created_filetime < 2**64
            or type(process.started_tick_ns) is not int
            or type(process.finished_tick_ns) is not int
            or not 0 <= process.started_tick_ns <= process.finished_tick_ns
            or type(native.connection_epoch) is not int
            or not 0 < native.connection_epoch < 2**63):
        errors.append("INVALID_PROCESS_OR_CONNECTION_RECORD")
    if native.image_path != policy.image_path or native.token_user != "S-1-5-19":
        errors.append("IMAGE_OR_ACCOUNT_MISMATCH")
    ident = native.image_identity
    if (type(ident) is not tuple or len(ident) != 3
            or type(ident[0]) is not int or not 0 < ident[0] < 2**64
            or type(ident[1]) is not str or len(ident[1]) != 32
            or any(c not in "0123456789abcdef" for c in ident[1])
            or ident[1] == "0" * 32
            or type(ident[2]) is not int or not 0 < ident[2] < 2**63):
        errors.append("INVALID_IMAGE_IDENTITY")
    for groups, restricted in ((native.service_groups, False),
                               (native.restricted_service_groups, True)):
        if (type(groups) is not tuple or len(groups) != 1
                or type(groups[0]) is not tuple or len(groups[0]) != 2
                or groups[0][0] != policy.service_sid
                or type(groups[0][1]) is not int or not 0 <= groups[0][1] <= 0xffffffff
                or (not restricted and (not groups[0][1] & 4 or groups[0][1] & 0x10))):
            errors.append("REGISTERED_SERVICE_SID_MISMATCH")
    return AuthenticationPrerequisites(not errors, tuple(errors),
        (policy.installation_id, policy.generation, policy.release_sequence))
