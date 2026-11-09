"""DEV_ONLY Win32 SCM/process snapshot; ALWAYS NOT_AUTHENTICATED.

Smaller B04-a contract: client-side GetNamedPipeServerProcessId semantics
remain unaccepted. No pipe HANDLE/PID/payload is accepted, opened or read.
Import is inert; DLL binding is lazy. This is not a service verifier.
"""
import ctypes
from dataclasses import dataclass, field
import os
import threading
import time

# Candidate name only; no installed service or protected registration asserted.
SERVICE_NAME = "SoraCommanderNextProtectedLedger"
SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_STATUS = 0x0004
SERVICE_QUERY_CONFIG = 0x0001  # Added only for private retained-config inspection.
SERVICE_WIN32_OWN_PROCESS = 0x0010
SERVICE_RUNNING = 4
PROCESS_ACCESS = 0x00101000  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
WAIT_TIMEOUT = 258
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
INVALID_HANDLE = ctypes.c_void_p(-1).value


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [(name, DWORD) for name in (
        "dwServiceType", "dwCurrentState", "dwControlsAccepted",
        "dwWin32ExitCode", "dwServiceSpecificExitCode", "dwCheckPoint",
        "dwWaitHint", "dwProcessId", "dwServiceFlags")]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", DWORD), ("dwHighDateTime", DWORD)]


class ObservationError(RuntimeError):
    """No observation result; sanitized stage/error and cleanup diagnostics."""

    def __init__(self, code, stage, winerror=None, cleanup_failures=()):
        super().__init__(f"{code}: {stage}; NOT_AUTHENTICATED")
        self.code = code
        self.stage = stage
        self.winerror = winerror
        self.cleanup_failures = cleanup_failures


@dataclass(frozen=True, slots=True)
class ServiceProcessObservation:
    service_name: str
    pid: int
    created_filetime: int
    started_tick_ns: int
    finished_tick_ns: int
    status: str = field(default="SCM_PROCESS_SNAPSHOT_ONLY", init=False)
    authentication: str = field(default="NOT_AUTHENTICATED", init=False)
    pipe_server_process: str = field(default="UNVERIFIED_CLIENT_HANDLE_API", init=False)
    service_configuration: str = field(default="UNVERIFIED", init=False)
    service_sid_and_token: str = field(default="UNVERIFIED", init=False)
    protected_image_publisher_pin_trust: str = field(default="UNVERIFIED", init=False)
    pipe_sqos_dacl_locality: str = field(default="UNVERIFIED", init=False)
    image_file_identity: str = field(default="UNVERIFIED", init=False)
    process_lifetime_until_reply: str = field(default="UNVERIFIED", init=False)


class _NativeCalls:
    """Exact Win32 ABI, also bound against fake DLL functions in mock tests."""

    def __init__(self, advapi32, kernel32, last_error):
        def bind(dll, name, args, result):
            fn = getattr(dll, name)
            fn.argtypes = args
            fn.restype = result
            return fn

        self.open_scm = bind(advapi32, "OpenSCManagerW",
                             [ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD], HANDLE)
        self.open_service = bind(advapi32, "OpenServiceW",
                                 [HANDLE, ctypes.c_wchar_p, DWORD], HANDLE)
        self.query_status = bind(advapi32, "QueryServiceStatusEx",
                                 [HANDLE, ctypes.c_int32, ctypes.POINTER(ctypes.c_ubyte),
                                  DWORD, ctypes.POINTER(DWORD)], BOOL)
        self.close_service = bind(advapi32, "CloseServiceHandle", [HANDLE], BOOL)
        self.open_process = bind(kernel32, "OpenProcess", [DWORD, BOOL, DWORD], HANDLE)
        self.process_id = bind(kernel32, "GetProcessId", [HANDLE], DWORD)
        self.process_times = bind(kernel32, "GetProcessTimes",
                                  [HANDLE] + [ctypes.POINTER(FILETIME)] * 4, BOOL)
        self.wait = bind(kernel32, "WaitForSingleObject", [HANDLE, DWORD], DWORD)
        self.close_process = bind(kernel32, "CloseHandle", [HANDLE], BOOL)
        self.last_error = last_error


def _load_native():
    if os.name != "nt":
        raise ObservationError("PLATFORM_UNSUPPORTED", "bind")
    # System32-only lookup, no user path/dependency search or fallback.
    return _NativeCalls(
        ctypes.WinDLL("advapi32.dll", use_last_error=True, winmode=0x800),
        ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800),
        ctypes.get_last_error)


_slot = threading.Lock()  # One in-flight observation; no global native handles.
_cleanup_uncertain = False  # No automatic reset/reopen after uncertain close.


def _uint(value, maximum, stage, *, nonzero=False):
    if type(value) is not int or not int(nonzero) <= value <= maximum:
        raise ObservationError("INVALID_NATIVE_RESULT", stage)
    return value


def _failure(api, stage):
    error = _uint(api.last_error(), 0xFFFFFFFF, stage + ".last_error")
    code = ("ACCESS_DENIED" if error == 5 else "RESOURCE_EXHAUSTED"
            if error in (4, 8, 14, 1450, 1816) else "WIN32_FAILURE")
    raise ObservationError(code, stage, error)


def _boolean(api, result, stage):
    # Win32 BOOL permits any nonzero signed 32-bit value, not only 1.
    if type(result) is not int or not -0x80000000 <= result <= 0x7FFFFFFF:
        raise ObservationError("INVALID_NATIVE_RESULT", stage)
    if result == 0:
        _failure(api, stage)


def _handle(api, result, stage):
    if result is None or type(result) is int and result == 0:
        _failure(api, stage)
    if (type(result) is not int or not 0 < result < INVALID_HANDLE
            or result >= INVALID_HANDLE - 15):
        raise ObservationError("INVALID_NATIVE_RESULT", stage)
    return result


def observe_service_process(*, timeout_ms=1000, _calls=None):
    """Read fixed local service/process only. No pipe correlation/authentication.

    _calls is a mock seam, not serialized identity evidence. Caller must not
    use this snapshot as permission. No retained HANDLE escapes the function.
    timeout_ms is a result deadline, not a guarantee that Win32 calls return.
    """
    return _observe_service_process(timeout_ms=timeout_ms, _calls=_calls)


def _observe_service_process(*, timeout_ms=1000, _calls=None, _inspect=None,
                             _inspect_service=None, _loader=None):
    """Internal extension: inspect ONLY while the original process is retained.

    The process callback receives a borrowed HANDLE and deadline checker.
    The private service callback receives both borrowed service and process
    HANDLEs during the same retained lifetime. When supplied, the service
    is opened with QUERY_STATUS | QUERY_CONFIG (read-only) rather than
    QUERY_STATUS alone. Neither callback may close/export the handles or
    grant authorization. The public
    observation API deliberately accepts no callback or externally claimed PID.
    """
    global _cleanup_uncertain
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 5000:
        raise ObservationError("INVALID_INPUT", "timeout_ms")
    if _calls is not None and type(_calls) is not _NativeCalls:
        raise ObservationError("INVALID_INPUT", "native_calls")
    # Allocation failure here cannot strand the admission lock.
    owned = [None, None, None]
    if not _slot.acquire(blocking=False):
        raise ObservationError("RESOURCE_EXHAUSTED", "observation_slot")
    api = None
    primary = None
    stage = "admission"
    try:
        if _cleanup_uncertain:
            raise ObservationError("CLEANUP_UNCONFIRMED", stage)
        start = time.monotonic_ns()
        deadline = start + timeout_ms * 1_000_000
        previous_tick = start

        def check_time():
            nonlocal previous_tick
            tick = time.monotonic_ns()
            if type(tick) is not int or tick < previous_tick or tick >= deadline:
                raise ObservationError("DEADLINE_OR_CLOCK_INVALID", stage)
            previous_tick = tick
            return tick

        api = _calls if _calls is not None else (_loader or _load_native)()
        check_time()
        stage = "OpenSCManagerW"
        owned[0] = api.open_scm(None, None, SC_MANAGER_CONNECT)
        scm = _handle(api, owned[0], stage)
        check_time()
        stage = "OpenServiceW"
        service_rights = (SERVICE_QUERY_STATUS | SERVICE_QUERY_CONFIG
                          if _inspect_service is not None else SERVICE_QUERY_STATUS)
        owned[1] = api.open_service(scm, SERVICE_NAME, service_rights)
        service = _handle(api, owned[1], stage)
        check_time()

        def status():
            check_time()
            data = SERVICE_STATUS_PROCESS()
            needed = DWORD()
            _boolean(api, api.query_status(
                service, 0, ctypes.cast(ctypes.byref(data), ctypes.POINTER(ctypes.c_ubyte)),
                ctypes.sizeof(data), ctypes.byref(needed)), "QueryServiceStatusEx")
            # pcbBytesNeeded is specified on INSUFFICIENT_BUFFER only; do not
            # invent a success-length contract. Fixed struct, no resize/retry.
            check_time()
            if (data.dwServiceType != SERVICE_WIN32_OWN_PROCESS
                    or data.dwCurrentState != SERVICE_RUNNING
                    or data.dwServiceFlags != 0 or data.dwWin32ExitCode != 0
                    or data.dwServiceSpecificExitCode != 0
                    or data.dwCheckPoint != 0 or data.dwWaitHint != 0):
                raise ObservationError("SERVICE_STATE_AMBIGUOUS", "QueryServiceStatusEx")
            _uint(data.dwProcessId, 0xFFFFFFFF, "service_pid", nonzero=True)
            return bytes(data)

        stage = "QueryServiceStatusEx.initial"
        initial = status()
        pid = SERVICE_STATUS_PROCESS.from_buffer_copy(initial).dwProcessId
        stage = "OpenProcess"
        owned[2] = api.open_process(PROCESS_ACCESS, 0, pid)
        process = _handle(api, owned[2], stage)
        check_time()

        def process_sample():
            check_time()
            wait = _uint(api.wait(process, 0), 0xFFFFFFFF, "WaitForSingleObject")
            if wait == 0xFFFFFFFF:
                _failure(api, "WaitForSingleObject")
            if wait != WAIT_TIMEOUT:
                raise ObservationError("PROCESS_EXITED_OR_UNKNOWN", "WaitForSingleObject")
            actual = _uint(api.process_id(process), 0xFFFFFFFF, "GetProcessId")
            if actual == 0:
                _failure(api, "GetProcessId")
            if actual != pid:
                raise ObservationError("PROCESS_CHANGED", "GetProcessId")
            created, exited, kernel, user = (FILETIME() for _ in range(4))
            _boolean(api, api.process_times(process, ctypes.byref(created),
                     ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)),
                     "GetProcessTimes")
            stamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
            # Windows documents lpExitTime as undefined for a process that
            # has not exited. Liveness comes from the retained process HANDLE
            # and WAIT_TIMEOUT, not an assumed zero exit FILETIME.
            if stamp == 0:
                raise ObservationError("PROCESS_TIME_AMBIGUOUS", "GetProcessTimes")
            check_time()
            return stamp

        stage = "process.initial"
        stamp = process_sample()
        if _inspect_service is not None:
            stage = "service.retained_config"
            _inspect_service(service, process, check_time)
            check_time()
        if _inspect is not None:
            stage = "process.prerequisites"
            _inspect(process, check_time)
            check_time()
        stage = "QueryServiceStatusEx.final"
        if status() != initial:
            raise ObservationError("SERVICE_CHANGED", stage)
        stage = "process.final"
        if process_sample() != stamp:
            raise ObservationError("PROCESS_CHANGED", stage)
        stage = "WaitForSingleObject.final"
        wait = _uint(api.wait(process, 0), 0xFFFFFFFF, stage)
        if wait == 0xFFFFFFFF:
            _failure(api, stage)
        if wait != WAIT_TIMEOUT:
            raise ObservationError("PROCESS_EXITED_OR_UNKNOWN", stage)
        finish = check_time()
        observation = ServiceProcessObservation(SERVICE_NAME, pid, stamp, start, finish)
    except BaseException as exc:
        primary = exc
        if isinstance(exc, (ObservationError, KeyboardInterrupt, SystemExit)):
            raise
        code = "RESOURCE_EXHAUSTED" if isinstance(exc, MemoryError) else "NATIVE_EXCEPTION"
        primary = ObservationError(code, stage)
        raise primary from None
    finally:
        failures = []
        if api is not None:
            for index in (2, 1, 0):
                value = owned[index]
                # No close for NULL/invalid/pseudo/malformed return values.
                if (type(value) is not int or not 0 < value < INVALID_HANDLE - 15):
                    continue
                close = api.close_process if index == 2 else api.close_service
                close_stage = "CloseHandle" if index == 2 else "CloseServiceHandle"
                try:
                    _boolean(api, close(value), close_stage)
                except BaseException:
                    failures.append((close_stage, index, "CLOSE_UNCONFIRMED"))
        if failures:
            _cleanup_uncertain = True
        _slot.release()
        if failures:
            if primary is None:
                raise ObservationError("CLEANUP_UNCONFIRMED", "cleanup",
                                       cleanup_failures=tuple(failures))
            if isinstance(primary, ObservationError):
                primary.cleanup_failures += tuple(failures)
            else:
                primary.add_note("B04-a cleanup unconfirmed; further observations blocked")
    # Cleanup is part of observation: late closes cannot yield a timely result.
    stage = "cleanup.deadline"
    check_time()
    return observation
