"""B04 DEV_ONLY retained-handle bridge: observe SCM configuration and process.

Combines the existing B04-a retained process HANDLE observer with the B04 SCM
QueryServiceConfigW/Config2W adapter *inside the same service HANDLE lifetime*.
Mock Win32 backends are used in all tests. An explicitly injected real backend
would require separate approval, native acceptance and independent trust proof.
All outputs remain NOT_AUTHENTICATED.
No connected client pipe, Publisher pin, protected installer or independent
release identity proof is established by this data bridge.
"""
from dataclasses import dataclass, field

from . import offline_b04_service_identity_probe as base
from .service_scm_sampling_candidate import ScmSamplingError, _snapshot
from .service_scm_query_abi_candidate import ExplicitScmQueryBackend
from .service_auth_candidate import ServiceConfigurationRecord


@dataclass(frozen=True, slots=True)
class RetainedScmProcessCandidate:
    process: base.ServiceProcessObservation
    initial: ServiceConfigurationRecord
    final: ServiceConfigurationRecord
    backend_reported_cleanup_success: bool = field(default=True, init=False)
    native_handle_cleanup_verified: bool = field(default=False, init=False)
    authentication: str = field(default="NOT_AUTHENTICATED", init=False)
    admission: str = field(default="UNAVAILABLE", init=False)
    connected_pipe_identity_verified: bool = field(default=False, init=False)
    publisher_trust_verified: bool = field(default=False, init=False)
    protected_install_verified: bool = field(default=False, init=False)
    image_and_config_live_epoch_verified: bool = field(default=False, init=False)
    missing_evidence: tuple = field(default=(
        "WIN32_NATIVE_BACKEND_NOT_ACCEPTED",
        "CLIENT_PIPE_HANDLE_SERVICE_PID_NOT_OS_BOUND",
        "INDEPENDENT_PUBLISHER_PIN_NOT_VERIFIED",
        "CONFIG_IS_NEXT_START_REGISTRY_STATE_NOT_RUNNING_IMAGE",
        "CONFIG_SAMPLING_ABA_CANNOT_BE_EXCLUDED",
        "PROTECTED_INSTALLATION_AND_RELEASE_GENERATION_NOT_OS_BOUND",
        "PERSISTENT_HANDLE_LIFETIME_THROUGH_AUTHENTICATED_REPLY_ABSENT",
    ), init=False)


def observe_retained_config_process(*, _calls=None, _config_backend=None,
                                    timeout_ms=1000):
    """Observe both on retained handles with two config samples (mocks only).

    Deliberately no default native loader and no field that can authorize a
    launch. A fake DLL/injected backend does not prove service identity or
    native handle cleanup beyond the injected close-call result.
    Live acceptance requires independent installer/Publisher/pipe validation.
    """
    if type(_calls) is not base._NativeCalls:
        raise base.ObservationError("NATIVE_BACKEND_NOT_ACCEPTED", "retained_bridge")
    if type(_config_backend) is not ExplicitScmQueryBackend:
        raise base.ObservationError("CONFIG_BACKEND_NOT_ACCEPTED", "retained_bridge")
    captured = []

    def inspect(service, process, check):
        if (type(service) is not int or type(process) is not int or
                service <= 0 or process <= 0 or service == process):
            raise base.ObservationError("INVALID_BORROWED_HANDLE", "retained_bridge")
        check()
        try:
            first = _snapshot(_config_backend, service)
            check()
            final = _snapshot(_config_backend, service)
            check()
        except ScmSamplingError as exc:
            raise base.ObservationError("CONFIG_QUERY_FAILED", exc.code) from exc
        if first != final:
            raise base.ObservationError("SERVICE_CONFIG_CHANGED", "retained_bridge")
        captured.append((first, final))

    process = base._observe_service_process(
        timeout_ms=timeout_ms, _calls=_calls, _inspect_service=inspect)
    if len(captured) != 1:
        raise base.ObservationError("RETAINED_CONFIG_NOT_OBSERVED", "retained_bridge")
    return RetainedScmProcessCandidate(process, *captured[0])
