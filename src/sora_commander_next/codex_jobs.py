"""Local Owner-only, read-only reconciliation of existing codex-bell records.

No launch, adoption, replay, cancellation, record repair, MCP registration or
background lifetime. A persisted PID is an observation, never control authority.
"""

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import re
import stat

MAX_RECORD_BYTES = 65536
_ID = re.compile(r"[0-9a-f]{32}\Z")
_UTC = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,7}))?(?:Z|\+00:00)\Z")
_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


class RecordError(ValueError):
    pass


def validate_job_id(job_id):
    if type(job_id) is not str or _ID.fullmatch(job_id) is None:
        raise RecordError("invalid job ID")


def _ticks(value):
    """Keep .NET's seventh fractional digit for exact Windows PID identity."""
    match = _UTC.fullmatch(value) if type(value) is str else None
    if match is None:
        raise RecordError("invalid UTC timestamp")
    try:
        delta = datetime.fromisoformat(match[1]).replace(tzinfo=timezone.utc) - _EPOCH
    except ValueError:
        raise RecordError("invalid UTC timestamp") from None
    if delta.days < 0:
        raise RecordError("invalid UTC timestamp")
    return ((delta.days * 86400 + delta.seconds) * 10_000_000
            + int((match[2] or "").ljust(7, "0")))


def _integer(value, low=0, high=2**63 - 1):
    if type(value) is not int or not low <= value <= high:
        raise RecordError("invalid integer")
    return value


def _validate(record, job_id):
    if (type(record) is not dict or type(record.get("schemaVersion")) is not int
            or record["schemaVersion"] != 2 or record.get("jobId") != job_id):
        raise RecordError("unsupported record")
    status = record.get("status")
    if type(status) is not str or status not in {"STARTING", "RUNNING", "SUCCESS", "FAILED", "UNKNOWN"}:
        raise RecordError("invalid status")
    created = _ticks(record.get("createdAtUtc"))
    updated = _ticks(record.get("updatedAtUtc"))
    if updated < created:
        raise RecordError("invalid lifecycle")
    _integer(record.get("wrapperProcessId"), 1, 2**32 - 1)
    _ticks(record.get("wrapperProcessStartTimeUtc"))
    started = record.get("codexStarted")
    if type(started) is not bool:
        raise RecordError("invalid launch flag")
    if started:
        _integer(record.get("codexProcessId"), 1, 2**32 - 1)
        _ticks(record.get("codexProcessStartTimeUtc"))
        if record["codexProcessId"] == record["wrapperProcessId"]:
            raise RecordError("invalid process identities")
        if not created <= _ticks(record.get("startedAtUtc")) <= updated:
            raise RecordError("invalid lifecycle")
    elif any(record.get(k) is not None for k in ("codexProcessId", "codexProcessStartTimeUtc", "startedAtUtc")):
        raise RecordError("inconsistent launch")
    if status in {"RUNNING", "SUCCESS"} and not started:
        raise RecordError("launch not proven")
    if status in {"STARTING", "RUNNING"}:
        if record.get("endedAtUtc") is not None or record.get("exitCode") is not None:
            raise RecordError("inconsistent active record")
    else:
        ended = _ticks(record.get("endedAtUtc"))
        if not created <= ended <= updated or (started and ended < _ticks(record["startedAtUtc"])):
            raise RecordError("invalid terminal lifecycle")
    exit_code = record.get("exitCode")
    if exit_code is not None:
        _integer(exit_code, -(2**31), 2**32 - 1)
    evidence = record.get("evidence")
    diagnostics = record.get("diagnostics")
    if type(evidence) is not dict or type(diagnostics) is not dict:
        raise RecordError("missing evidence")
    for key in ("turnStarted", "turnCompleted", "turnFailed", "errorEvent"):
        if type(evidence.get(key)) is not bool:
            raise RecordError("invalid evidence flag")
    for key in ("stdoutBytes", "stdoutLines", "malformedLines", "truncatedLines", "oversizedLines"):
        _integer(evidence.get(key))
    for key in ("cleanupErrorCount", "stateWriteFailures"):
        _integer(diagnostics.get(key))
    if type(diagnostics.get("childMayStillBeRunning")) is not bool:
        raise RecordError("invalid cleanup flag")
    if status == "SUCCESS" and (exit_code != 0 or not evidence["turnCompleted"]
            or evidence["turnFailed"] or evidence["errorEvent"]
            or any(evidence[k] for k in ("malformedLines", "truncatedLines", "oversizedLines"))):
        raise RecordError("completion not proven")
    if status == "FAILED" and not ((exit_code is not None and exit_code != 0) or evidence["turnFailed"]):
        raise RecordError("failure not proven")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise RecordError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise RecordError("invalid JSON constant")


@contextmanager
def _snapshot(path):
    """Pin every Windows directory against rename; reject all reparse points.

    File opens deny writes/deletes for the short bounded read. A racing wrapper
    replacement may make this query or that save fail; no retry or repair here.
    This is not a boundary against code running as the same Windows Owner.
    """
    import ctypes
    from ctypes import wintypes as w
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE)
    create.restype = w.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = (w.HANDLE,), w.BOOL
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = (w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD)
    info.restype = w.BOOL

    class Attributes(ctypes.Structure):
        _fields_ = [("attributes", w.DWORD), ("tag", w.DWORD)]

    with ExitStack() as stack:
        for part in (*reversed(path.parents), path):
            directory = part != path
            handle = create(str(part), 0x80 if directory else 0x80000000,
                            3 if directory else 1, None, 3,
                            0x00200000 | (0x02000000 if directory else 0), None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            # Ownership transfers to the file descriptor only after validation.
            try:
                attrs = Attributes()
                if not info(handle, 9, ctypes.byref(attrs), ctypes.sizeof(attrs)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if attrs.attributes & 0x400 or bool(attrs.attributes & 0x10) != directory:
                    raise RecordError("linked or invalid record path")
                if directory:
                    stack.callback(close, handle)
                else:
                    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                    handle = None
                    stream = stack.enter_context(os.fdopen(fd, "rb"))
            except BaseException:
                if handle is not None:
                    close(handle)
                raise
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_size > MAX_RECORD_BYTES:
            raise RecordError("invalid record file")
        yield stream


def _read_record(path):
    with _snapshot(path) as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise RecordError("oversized record")
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs,
                          parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise RecordError("invalid record JSON") from None


def _process_identity(pid, expected_ticks):
    """Read-only PID + creation FILETIME + exit observation on ONE handle."""
    import ctypes
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel.OpenProcess
    open_process.argtypes, open_process.restype = (w.DWORD, w.BOOL, w.DWORD), w.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = (w.HANDLE,), w.BOOL
    times = kernel.GetProcessTimes
    times.argtypes = (w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4))
    times.restype = w.BOOL
    wait = kernel.WaitForSingleObject
    wait.argtypes, wait.restype = (w.HANDLE, w.DWORD), w.DWORD
    handle = open_process(0x1000 | 0x00100000, False, pid)
    if not handle:
        return "MISSING" if ctypes.get_last_error() == 87 else "UNAVAILABLE"
    try:
        values = [w.FILETIME() for _ in range(4)]
        if not times(handle, *(ctypes.byref(v) for v in values)):
            return "UNAVAILABLE"
        actual = (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
        if actual != expected_ticks:
            return "PID_REUSED"
        result = wait(handle, 0)
        return {0: "EXITED", 0x102: "MATCH"}.get(result, "UNAVAILABLE")
    finally:
        close(handle)


def _reconcile(record, job_id):
    _validate(record, job_id)
    wrapper = _process_identity(record["wrapperProcessId"], _ticks(record["wrapperProcessStartTimeUtc"]))
    codex = (_process_identity(record["codexProcessId"], _ticks(record["codexProcessStartTimeUtc"]))
             if record["codexStarted"] else "NOT_STARTED")
    status, reason = "UNKNOWN", "SUPERVISION_NOT_PROVEN"
    observations = {wrapper, codex}
    if "UNAVAILABLE" in observations:
        reason = "PROCESS_OBSERVATION_UNAVAILABLE"
    elif record["status"] in {"STARTING", "RUNNING"}:
        if "PID_REUSED" in observations:
            reason = "PROCESS_IDENTITY_MISMATCH"
        elif wrapper in {"MISSING", "EXITED"}:
            reason = "WRAPPER_MISSING"
        elif codex in {"MISSING", "EXITED"}:
            reason = "CODEX_MISSING"
    elif (record["diagnostics"]["childMayStillBeRunning"]
            or record["diagnostics"]["stateWriteFailures"]
            or record["diagnostics"]["cleanupErrorCount"]):
        reason = "WRAPPER_DIAGNOSTICS_UNCERTAIN"
    elif "MATCH" in observations:
        reason = "FINALIZATION_NOT_QUIESCENT"
    else:
        status, reason = record["status"], "RECORDED_TERMINAL"
    return {"job_id": job_id, "source": "codex-bell", "recorded_status": record["status"],
            "status": status, "reason": reason, "wrapper_process": wrapper,
            "codex_process": codex, "exit_code": record["exitCode"],
            "supervision_verified": False, "state_mutated": False,
            "task_acceptance_verified": False, "auto_retry": False,
            "next_step": ("VERIFY_ARTIFACTS_AND_TESTS" if status == "SUCCESS"
                          else "INSPECT_EXISTING_JOB_MANUALLY")}


class LocalCodexJobs:
    """Explicit local invocation only; no root/SID/provider supplied by a client."""

    def __init__(self):
        if os.name != "nt":
            raise PermissionError("Windows Owner session required")
        from .windows_owner import OwnerSessionGuard
        self._guard = OwnerSessionGuard()
        self._guard.verify()
        base = Path(os.environ["LOCALAPPDATA"])
        if not base.is_absolute() or base.drive.startswith("\\\\") or ".." in base.parts:
            raise RecordError("invalid local storage")
        self._root = base / "CodexJobNotify" / "jobs"

    def query(self, job_id):
        self._guard.verify()
        validate_job_id(job_id)
        result = _reconcile(_read_record(self._root / (job_id + ".json")), job_id)
        self._guard.verify()
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local Owner-only codex-bell query; never launches or repairs jobs")
    parser.add_argument("job_id")
    args = parser.parse_args(argv)
    try:
        result = LocalCodexJobs().query(args.job_id)
    except PermissionError:
        result = {"status": "ERROR", "error": "OWNER_OR_READ_ACCESS_REQUIRED"}
    except (OSError, ValueError, KeyError, RuntimeError):
        result = {"status": "ERROR", "error": "RECORD_UNAVAILABLE_OR_INVALID"}
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] in {"SUCCESS", "FAILED"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
