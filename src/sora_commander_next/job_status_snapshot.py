"""Trusted local, metadata-only adapters for a future CLI monitor.

No UI, IPC, authentication, process discovery or lifecycle ownership is added.
Unlike JobManager.list_sessions(), capture does not prune retained jobs or poll
children. Expired entries remain visible until existing lifecycle code prunes
them. Status is the supervisor's recorded observation, not CLI task acceptance
or proof of live supervision. Unknown states are preserved, never promoted.
"""
from dataclasses import dataclass

from .jobs import JobManager


@dataclass(frozen=True)
class JobStatus:
    session_id: str
    status: str
    exit_code: int | None
    captured_bytes: int
    output_incomplete: bool


@dataclass(frozen=True)
class JobStatusSnapshot:
    sessions: tuple[JobStatus, ...]

    @property
    def count(self) -> int:
        return len(self.sessions)


MAX_MONITOR_ROWS = 256


@dataclass(frozen=True)
class JobMonitorRow:
    session_id: str
    state: str
    exit_code: int | None
    captured_bytes: int
    output_incomplete: bool


@dataclass(frozen=True)
class JobMonitorView:
    sessions: tuple[JobMonitorRow, ...]
    total_count: int

    @property
    def truncated(self) -> bool:
        return len(self.sessions) < self.total_count


def monitor_view(snapshot: JobStatusSnapshot, *, limit: int = 32) -> JobMonitorView:
    """Pure, bounded presentation of an already captured local snapshot.

    COMPLETED means recorded process exit zero with complete captured output,
    not Codex turn acceptance, fresh liveness or verified supervision. Explicit
    failure, cancellation, timeout and output-limit states display as FAILED.
    Missing/contradictory exit evidence and unfamiliar states stay UNKNOWN.
    No raw status strings, commands, output, paths or process IDs are exposed.
    Capture errors must propagate at the caller; they are not empty views.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_MONITOR_ROWS:
        raise ValueError("invalid monitor row limit")
    if type(snapshot) is not JobStatusSnapshot or type(snapshot.sessions) is not tuple:
        raise TypeError("existing job snapshot required")
    rows = []
    for row in snapshot.sessions[:limit]:
        if (type(row) is not JobStatus
                or type(row.session_id) is not str or len(row.session_id) != 48
                or any(c not in "0123456789abcdef" for c in row.session_id)
                or type(row.captured_bytes) is not int
                or not 0 <= row.captured_bytes <= 2**63 - 1
                or type(row.output_incomplete) is not bool):
            raise ValueError("invalid job metadata")
        valid_exit = (row.exit_code is None or (type(row.exit_code) is int
                      and -2**31 <= row.exit_code <= 2**32 - 1))
        state = "UNKNOWN"
        if type(row.status) is str and valid_exit:
            if row.status == "RUNNING" and row.exit_code is None:
                state = "RUNNING"
            elif row.status == "COMPLETED" and row.exit_code is not None:
                if row.exit_code != 0:
                    state = "FAILED"
                elif not row.output_incomplete:
                    state = "COMPLETED"
            elif row.status in {"FAILED", "CANCELLED", "TIMEOUT", "OUTPUT_LIMIT", "ERROR"}:
                state = "FAILED"
        rows.append(JobMonitorRow(row.session_id, state,
                                  row.exit_code if valid_exit else None,
                                  row.captured_bytes, row.output_incomplete))
    return JobMonitorView(tuple(rows), snapshot.count)


class JobStatusSnapshotAdapter:
    """In-memory read access only; callers must supply an existing manager.

    Uses JobManager's internal locks because its public list/read APIs perform
    retention cleanup. Membership is stable during capture; each row is copied
    under its job lock. Rows do not imply one simultaneous process observation.
    This local helper supplies no remote access or Owner authorization boundary.
    """

    def __init__(self, manager: JobManager):
        if not isinstance(manager, JobManager):
            raise TypeError("existing JobManager required")
        self._manager = manager

    def snapshot(self) -> JobStatusSnapshot:
        manager = self._manager
        sessions = []
        with manager._lock:
            for job in manager._jobs.values():
                with job.lock:
                    sessions.append(JobStatus(
                        session_id=job.session_id,
                        status=job.status,
                        exit_code=job.exit_code,
                        captured_bytes=job.captured,
                        output_incomplete=job.output_incomplete,
                    ))
        return JobStatusSnapshot(tuple(sessions))
