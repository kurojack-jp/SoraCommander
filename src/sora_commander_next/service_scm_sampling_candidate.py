"""DEV_ONLY B04 same-SCM-handle configuration sampling (mock backend only).

No Win32 loader, service opening, token queries, IPC, trust validation or
execution code exists here. Backend values are mere observations and cannot
authorize native service traffic. This is a contract for future native review.
"""
from dataclasses import dataclass, field
from time import monotonic_ns

from .offline_b04_service_identity_probe import SERVICE_NAME
from .service_auth_candidate import ServiceConfigurationRecord
from .offline_b01_full_binding import ContractError

SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_CONFIG = 0x0001
MAX_TIMEOUT_MS = 3000
LIMIT_HANDLE = (1 << 64) - 16


class ScmSamplingError(RuntimeError):
    def __init__(self, code, *, cleanup=()):
        super().__init__(code + "; NOT_AUTHENTICATED")
        self.code = code
        self.cleanup = tuple(cleanup)


@dataclass(frozen=True, slots=True)
class ScmConfigurationSamples:
    first: ServiceConfigurationRecord
    last: ServiceConfigurationRecord
    elapsed_ms: int
    unchanged: bool
    authentication: str = field(default="NOT_AUTHENTICATED", init=False)
    admission: str = field(default="UNAVAILABLE", init=False)
    native_service_handle_verified: bool = field(default=False, init=False)
    connected_client_pipe_bound: bool = field(default=False, init=False)
    publisher_pinned: bool = field(default=False, init=False)
    installed_release_verified: bool = field(default=False, init=False)
    service_process_retained: bool = field(default=False, init=False)
    missing_evidence: tuple = field(default=(
        "SCM_READ_BACKEND_NATIVE_ABI_NOT_ACCEPTED",
        "SCM_SERVICE_HANDLE_NOT_BOUND_TO_RETAINED_PROCESS",
        "CONNECTED_CLIENT_PIPE_HANDLE_PEER_PID_UNVERIFIED",
        "WINVERIFYTRUST_AND_INDEPENDENT_PUBLISHER_PIN_ABSENT",
        "SERVICE_CONFIG_HANDLE_LIFETIME_NOT_OS_VERIFIED",
    ), init=False)


class ScmBackend:
    """Injection interface; implementing it does not grant service authority.

    open_scm(access), open_service(scm_handle, service_name, access),
    query_config(service_handle) -> (binary_path, account, type, start, error),
    query_sid_type(service_handle) -> DWORD, close(handle) -> literal True.
    Native ctypes binding intentionally deferred pending independent ABI review.
    """


def _handle(value):
    if type(value) is not int or not 0 < value < LIMIT_HANDLE:
        raise ScmSamplingError("INVALID_SCM_HANDLE")
    return value


def _snapshot(backend, handle):
    primary = backend.query_config(handle)
    if type(primary) is not tuple or len(primary) != 5:
        raise ScmSamplingError("INVALID_CONFIG_SHAPE")
    sid_type = backend.query_sid_type(handle)
    if type(sid_type) is not int or not 0 <= sid_type < 1 << 32:
        raise ScmSamplingError("INVALID_SID_TYPE")
    try:
        record = ServiceConfigurationRecord(SERVICE_NAME, *primary, sid_type)
        record.__post_init__()
    except (ContractError, TypeError, ValueError) as exc:
        raise ScmSamplingError("MALFORMED_CONFIG_SAMPLE") from exc
    return record


def sample_scm_configuration(*, _backend=None, _clock_ms=None, timeout_ms=1000):
    """Capture two snapshots on ONE held service handle; never authenticate.

    Fail closed on partial open, clock rollback, late callback, mismatched
    observations or uncertain close. No retry, repair, enable/start or callback
    capable of granting a service permission. Only fake backends are currently
    supplied/tested. A future native binding requires separate authorization.
    """
    if _backend is None:
        raise ScmSamplingError("NATIVE_BACKEND_NOT_ACCEPTED")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
        raise ScmSamplingError("INVALID_TIMEOUT")
    clock = _clock_ms if _clock_ms is not None else lambda: monotonic_ns() // 1_000_000
    owned = []
    first = last = None
    last_tick = -1
    start = deadline = None
    primary = None
    cleanup = []

    def tick():
        nonlocal last_tick, start, deadline
        current = clock()
        if type(current) is not int or not 0 <= current < 1 << 63:
            raise ScmSamplingError("INVALID_CLOCK")
        if current < last_tick:
            raise ScmSamplingError("CLOCK_ROLLBACK")
        if start is None:
            start, deadline = current, current + timeout_ms
        if current >= deadline:
            raise ScmSamplingError("SCM_READ_DEADLINE")
        last_tick = current
        return current

    try:
        tick()
        scm = _handle(_backend.open_scm(SC_MANAGER_CONNECT))
        owned.append(scm)
        tick()
        service = _handle(_backend.open_service(scm, SERVICE_NAME, SERVICE_QUERY_CONFIG))
        if service == scm:
            raise ScmSamplingError("ALISED_SCM_HANDLE")
        owned.append(service)
        tick()
        first = _snapshot(_backend, service)
        tick()
        last = _snapshot(_backend, service)
        tick()
        if first != last:
            raise ScmSamplingError("SCM_CONFIG_CHANGED")
    except BaseException as exc:
        primary = exc
    finally:
        for handle in reversed(owned):
            try:
                if _backend.close(handle) is not True:
                    cleanup.append("CLOSE_UNCONFIRMED")
            except BaseException:
                cleanup.append("CLOSE_FAILED")
    if cleanup:
        if isinstance(primary, (KeyboardInterrupt, SystemExit)):
            raise primary
        raise ScmSamplingError("SCM_HANDLE_CLEANUP_UNKNOWN", cleanup=cleanup) from primary
    if primary is not None:
        raise primary
    finish = tick()
    return ScmConfigurationSamples(first, last, finish - start, True)
