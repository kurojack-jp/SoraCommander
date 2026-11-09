"""Opt-in, metadata-only session journal for crash diagnostics, never job replay."""
import json
import os
from pathlib import Path

class JournalError(ValueError):
    pass

_FIELDS = {"session_id", "status", "pid", "exit_code", "captured_bytes", "output_incomplete"}
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
             "OUTPUT_LIMIT", "OUTPUT_INCOMPLETE", "ERROR", "OUTCOME_UNKNOWN"}

def _safe(entry):
    if type(entry) is not dict or set(entry) != _FIELDS:
        raise JournalError("invalid journal metadata")
    sid = entry["session_id"]
    if type(sid) is not str or len(sid) != 48 or any(c not in "0123456789abcdef" for c in sid):
        raise JournalError("invalid session id")
    if entry["status"] not in _TERMINAL | {"RUNNING"}:
        raise JournalError("invalid status")
    if type(entry["pid"]) is not int or entry["pid"] < 0:
        raise JournalError("invalid pid")
    if entry["exit_code"] is not None and type(entry["exit_code"]) is not int:
        raise JournalError("invalid exit code")
    if type(entry["captured_bytes"]) is not int or not 0 <= entry["captured_bytes"] <= 16_777_216:
        raise JournalError("invalid byte count")
    if type(entry["output_incomplete"]) is not bool:
        raise JournalError("invalid output flag")
    return dict(entry)

def write_snapshot(path, entries, *, protected_stage=False):
    """Serialize cooperating Windows journal writers with a kernel byte lock.

    This is a single-writer pilot control, not a defense against an untrusted
    process that ignores the lock or manipulates directory entries.
    """
    from contextlib import contextmanager
    from pathlib import Path
    import os
    import stat
    p = Path(path)
    if os.name != "nt":
        return _write_snapshot_unlocked(p, entries, protected_stage=protected_stage)
    import msvcrt
    lock_path = p.with_name(p.name + ".lock")
    if (not p.is_absolute() or not p.parent.is_dir()
            or p.parent.is_symlink() or p.parent.is_junction()
            or lock_path.is_symlink() or lock_path.is_junction()):
        raise JournalError("unsafe journal lock path")
    if lock_path.exists():
        info = lock_path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise JournalError("invalid journal lock file")
    # The trusted Owner-only directory is validated by stdio_main before use.
    # A byte-range lock coordinates cooperating writers across processes.
    # In protected Owner mode create the lock EXCLUSIVELY so a racing file
    # cannot be silently opened; never rewrite an existing lock's ACL.
    existing_lock = lock_path.exists()
    lock_mode = ("r+b" if existing_lock else "x+b") if protected_stage else "a+b"
    with open(lock_path, lock_mode) as lock:
        if protected_stage:
            from .owner_stage_acl import (
                protect_owner_stage_local, verify_owner_stage_after_replace,
            )
            if existing_lock:
                verify_owner_stage_after_replace(lock_path)
            else:
                protect_owner_stage_local(lock_path, fresh_lock=True)
            verify_owner_stage_after_replace(lock_path)
        lock.seek(0)
        lock.write(b"\\0")
        lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise JournalError("journal writer already active") from exc
        try:
            return _write_snapshot_unlocked(p, entries, protected_stage=protected_stage)
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _write_snapshot_unlocked(path, entries, *, protected_stage=False):
    """Explicit trusted local path; atomic replacement; no executable or argv stored."""
    p = Path(path)
    if not p.is_absolute() or p.is_symlink() or not p.parent.is_dir():
        raise JournalError("absolute existing directory required")
    if type(entries) is not list or len(entries) > 256:
        raise JournalError("too many sessions")
    safe = [_safe(e) for e in entries]
    if len({e["session_id"] for e in safe}) != len(safe):
        raise JournalError("duplicate sessions")
    raw = json.dumps({"version": 1, "sessions": safe}, separators=(",", ":")).encode("utf-8")
    if len(raw) > 65536:
        raise JournalError("snapshot too large")
    import tempfile
    import stat
    directory = p.parent
    if directory.is_symlink() or directory.is_junction():
        raise JournalError("linked journal directory")
    parent_identity = directory.stat()
    if not stat.S_ISDIR(parent_identity.st_mode):
        raise JournalError("invalid journal directory")
    if p.exists():
        before = p.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise JournalError("invalid existing journal")
    else:
        before = None
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=directory, prefix=".sora-session-", suffix=".tmp", delete=False) as f:
            temp = Path(f.name)
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        staged = temp.lstat()
        if (not stat.S_ISREG(staged.st_mode) or staged.st_nlink != 1
                or directory.is_symlink() or directory.is_junction()
                or (directory.stat().st_dev, directory.stat().st_ino)
                != (parent_identity.st_dev, parent_identity.st_ino)):
            raise JournalError("journal directory or stage changed")
        if p.is_symlink() or p.is_junction():
            raise JournalError("journal target became linked")
        if before is None:
            if p.exists():
                raise JournalError("journal target appeared")
        else:
            if not p.exists():
                raise JournalError("journal target disappeared")
            current = p.stat()
            if ((current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                    or current.st_nlink != 1):
                raise JournalError("journal target changed")
        if protected_stage:
            from .owner_stage_acl import protect_owner_stage_local
            protect_owner_stage_local(temp)
        os.replace(temp, p)
        if protected_stage:
            from .owner_stage_acl import verify_owner_stage_after_replace
            verify_owner_stage_after_replace(p)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)

def read_previous_snapshot(path):
    """Old RUNNING jobs are always UNKNOWN; journal is not an execution authority."""
    p = Path(path)
    if not p.is_absolute() or p.is_symlink() or not p.is_file() or p.stat().st_size > 65536:
        raise JournalError("invalid journal file")
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise JournalError("invalid journal contents") from exc
    if type(obj) is not dict or set(obj) != {"version", "sessions"} or obj["version"] != 1 or type(obj["sessions"]) is not list or len(obj["sessions"]) > 256:
        raise JournalError("invalid journal schema")
    entries = [_safe(e) for e in obj["sessions"]]
    for item in entries:
        if item["status"] == "RUNNING":
            item["status"] = "INTERRUPTED_UNKNOWN"
            item["exit_code"] = None
    return {"sessions": entries, "count": len(entries)}
