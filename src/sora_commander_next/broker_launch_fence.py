"""Durable, DATA ONLY request-ID replay denial; permanently HOLD_LIVE.

No approval, authentication, launch, retry permission, GUI or endpoint exists
here. Even ATTEMPT_RECORDED_DATA_ONLY is only a committed data observation.
The future independent protected Broker must verify code provenance, trusted
ACLs, Owner GUI approval and OS completion independently before using this.

Path checks are best effort, NOT same-user tamper resistance: deletion,
replacement, rollback of the database or races against path checks require a
separately accepted protected storage boundary. A successful SQLite FULL /
DELETE commit is the durability boundary, subject to honest OS/storage fsync.
An I/O error is denial, never proof of commit or permission to launch. Never
retry an uncertain launch or refund an existing claim after a storage error.
SQLite's temporary rollback journal is allowed; WAL/SHM files are not.
"""

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

from .commands import Command


_DOMAIN = b"sora-commander-next/broker-launch-fence/command/v1\0"
_APPLICATION_ID = 0x53434E46
_SCHEMA_VERSION = 1
_TIMEOUT_SECONDS = 1.0
_SCHEMA = """CREATE TABLE launch_fence (
    request_id TEXT PRIMARY KEY NOT NULL
        CHECK(length(request_id)=32 AND length(CAST(request_id AS BLOB))=32
              AND request_id NOT GLOB '*[^0-9a-f]*'),
    fingerprint TEXT NOT NULL
        CHECK(length(fingerprint)=64 AND length(CAST(fingerprint AS BLOB))=64
              AND fingerprint NOT GLOB '*[^0-9a-f]*'),
    status TEXT NOT NULL CHECK(status IN ('UNKNOWN','TERMINAL'))
) WITHOUT ROWID, STRICT"""
_EXPECTED_SCHEMA = [("table", "launch_fence", "launch_fence", _SCHEMA)]


class LaunchFenceError(ValueError):
    """Storage/path/schema is unusable. This is always a denial."""


@dataclass(frozen=True, slots=True)
class DataOnlyOutcome:
    """Only a data status; contains no capability, secret, path or process ID."""

    status: str


def _fingerprint(command):
    # Revalidate the frozen value, including instances forged with object APIs.
    if type(command) is not Command:
        raise ValueError("an immutable Command is required")
    if (type(command.request_id) is not str
            or re.fullmatch(r"[0-9a-f]{32}", command.request_id) is None):
        raise ValueError("request_id must be exactly 32 lowercase hex characters")
    for value in (command.executable, command.cwd):
        if (type(value) is not str or not value or "\0" in value
                or not Path(value).is_absolute()):
            raise ValueError("invalid absolute Command path")
    if (type(command.argv) is not tuple or len(command.argv) > 128
            or any(type(a) is not str or "\0" in a or len(a) > 8192
                   for a in command.argv)):
        raise ValueError("invalid immutable argv")
    try:
        valid_timeout = (type(command.timeout) in (int, float)
                         and math.isfinite(command.timeout)
                         and 0.01 <= command.timeout <= 3600)
    except (ValueError, OverflowError):
        valid_timeout = False
    if not valid_timeout:
        raise ValueError("invalid finite timeout")
    if type(command.output_limit) is not int or not 128 <= command.output_limit <= 65536:
        raise ValueError("invalid output limit")
    # Distinguish exact int/float representations without locale or float JSON
    # rounding. Sorted ASCII JSON preserves strings and argument boundaries.
    timeout = (["float", command.timeout.hex()] if type(command.timeout) is float
               else ["int", str(command.timeout)])
    encoded = json.dumps({
        "executable": command.executable, "argv": command.argv,
        "cwd": command.cwd, "timeout": timeout,
        "output_limit": command.output_limit, "request_id": command.request_id,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False).encode("ascii")
    return hashlib.sha256(_DOMAIN + encoded).hexdigest()


def _fixed_drive(root):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_type = kernel.GetDriveTypeW
    get_type.argtypes = [wintypes.LPCWSTR]
    get_type.restype = wintypes.UINT
    return get_type(root) == 3  # DRIVE_FIXED, not network/removable/unknown.


def _plain_stat(path, *, directory=False, missing=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise LaunchFenceError("parent or database disappeared") from None
    if (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or stat.S_ISLNK(info.st_mode)):
        raise LaunchFenceError("reparse points are forbidden")
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise LaunchFenceError("ordinary existing parent directory required")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise LaunchFenceError("ordinary single-link database file required")
    return info


def _local_path(value):
    if sys.platform != "win32":
        raise LaunchFenceError("launch fence storage is Windows-only")
    if type(value) is not str and not isinstance(value, Path):
        raise LaunchFenceError("absolute local file path required")
    raw = str(value)
    if re.match(r"^[A-Za-z]:\\", raw) is None:
        raise LaunchFenceError("absolute fixed-drive path required")
    parts = raw[3:].split("\\")
    for part in parts:
        if (not part or part in (".", "..") or part.endswith((".", " "))
                or re.search(r'[<>:"/|?*\x00-\x1f]', part)
                or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", part)):
            raise LaunchFenceError("ambiguous or special path component")
    if not _fixed_drive(raw[:3]):
        raise LaunchFenceError("fixed local drive required")
    return Path(raw)


class BrokerLaunchFence:
    """Local data primitive, intentionally not wired to BrokerOwnerProcess.

    Normal construction opens an existing, nonempty database only. Initial
    creation is an explicit data-only operation, never a recovery fallback.
    Existing files must match this exact format; no migrations, repair, reset,
    expiry or replacement. Neither entry point authenticates an administrator.
    Connections have a bounded one-second busy timeout and use one thread.
    """

    def __init__(self, path):
        self._open(path, provision=False)

    @classmethod
    def provision_new_data_only(cls, path):
        """Exclusively create a new data store; refuse EVERY existing file.

        Not an administrative authorization API. A future protected installer
        must enforce its own identity, registration and storage boundary.
        Failed creation is preserved for diagnosis, never deleted or retried.
        """
        instance = cls.__new__(cls)
        instance._open(path, provision=True)
        return instance

    def _open(self, path, *, provision):
        self._db = None
        self._identity = None
        self._path = _local_path(path)
        try:
            if provision:
                if self._check_path(missing=True) is not None:
                    raise LaunchFenceError("provision requires a new database")
                for suffix in ("-journal", "-wal", "-shm"):
                    if _plain_stat(Path(str(self._path) + suffix), missing=True) is not None:
                        raise LaunchFenceError("provision refuses orphan sidecars")
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                try:
                    info = os.fstat(fd)
                    self._identity = (info.st_dev, info.st_ino)
                finally:
                    os.close(fd)
            info = self._check_path()
            if not provision and info.st_size == 0:
                raise LaunchFenceError("empty database is not an initialized ledger")
            self._identity = (info.st_dev, info.st_ino)
            # URI mode=rw prevents silently creating a replacement on reopen.
            self._db = sqlite3.connect(self._path.as_uri() + "?mode=rw",
                                       uri=True, isolation_level=None,
                                       timeout=_TIMEOUT_SECONDS)
            self._db.execute("PRAGMA trusted_schema=OFF")
            self._db.execute("PRAGMA foreign_keys=ON")
            if self._db.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                raise LaunchFenceError("DELETE journal mode required")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("BEGIN IMMEDIATE")
            self._check_path()
            schema = self._db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema").fetchall()
            metadata = (self._db.execute("PRAGMA application_id").fetchone()[0],
                        self._db.execute("PRAGMA user_version").fetchone()[0])
            if provision:
                if schema or metadata != (0, 0):
                    raise LaunchFenceError("new database changed before initialization")
                self._db.execute(_SCHEMA)
                self._db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                self._db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._check_database()
            self._db.execute("COMMIT")
            self._check_path()
        except (sqlite3.Error, OSError, LaunchFenceError) as exc:
            self.close()
            raise LaunchFenceError("launch fence storage refused") from exc

    def _check_path(self, *, missing=False):
        for parent in reversed(self._path.parents):
            _plain_stat(parent, directory=True)
        info = _plain_stat(self._path, missing=missing)
        if (info is not None and self._identity is not None
                and (info.st_dev, info.st_ino) != self._identity):
            raise LaunchFenceError("database file identity changed")
        # SQLite can recover a valid hot DELETE journal. Never follow a link
        # or open WAL/SHM sidecars, including ones left by another connection.
        _plain_stat(Path(str(self._path) + "-journal"), missing=True)
        for suffix in ("-wal", "-shm"):
            if _plain_stat(Path(str(self._path) + suffix), missing=True) is not None:
                raise LaunchFenceError("unexpected SQLite sidecar")
        return info

    def _check_database(self):
        db = self._db
        if (db.execute("PRAGMA application_id").fetchone() != (_APPLICATION_ID,)
                or db.execute("PRAGMA user_version").fetchone() != (_SCHEMA_VERSION,)
                or db.execute("PRAGMA foreign_keys").fetchone() != (1,)
                or db.execute("PRAGMA synchronous").fetchone() != (2,)
                or db.execute("PRAGMA journal_mode").fetchone() != ("delete",)
                or db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema").fetchall()
                != _EXPECTED_SCHEMA
                or db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                or db.execute("PRAGMA foreign_key_check").fetchall()):
            raise LaunchFenceError("launch fence integrity/schema/metadata mismatch")

    def _operate(self, command, operation):
        fingerprint = _fingerprint(command)  # Bad inputs never touch storage.
        if self._db is None:
            return DataOnlyOutcome("DENY_STORAGE_ERROR")
        try:
            self._check_path()
            self._db.execute("BEGIN" if operation == "inspect" else "BEGIN IMMEDIATE")
            self._check_database()
            row = self._db.execute(
                "SELECT fingerprint,status FROM launch_fence WHERE request_id=?",
                (command.request_id,)).fetchone()
            if row is None:
                if operation == "reserve":
                    self._db.execute(
                        "INSERT INTO launch_fence VALUES (?,?,'UNKNOWN')",
                        (command.request_id, fingerprint))
                    status = "ATTEMPT_RECORDED_DATA_ONLY"
                else:
                    status = "NO_ATTEMPT_RECORDED_DATA_ONLY"
            elif row[0] != fingerprint:
                status = "DENY_DUPLICATE_CONFLICT"
            elif operation == "terminal" and row[1] == "UNKNOWN":
                # A caller must already possess independent trusted OS
                # completion evidence. This API neither supplies nor verifies
                # that evidence; the marker never permits another attempt.
                self._db.execute(
                    "UPDATE launch_fence SET status='TERMINAL' WHERE request_id=? AND status='UNKNOWN'",
                    (command.request_id,))
                status = "TERMINAL_RECORDED_DATA_ONLY"
            else:
                status = "ALREADY_ATTEMPTED_" + row[1]
            self._check_path()
            self._db.execute("COMMIT")
            # On any ambiguous post-commit failure retain the claim; poison
            # this instance. Never delete a row or compensate/refund a claim.
            self._check_path()
            return DataOnlyOutcome(status)
        except (sqlite3.Error, OSError, LaunchFenceError):
            self.close()
            return DataOnlyOutcome("DENY_STORAGE_ERROR")

    def inspect_data_only(self, command):
        """Read a snapshot only; absence never implies permission to launch."""
        return self._operate(command, "inspect")

    def reserve_attempt_data_only(self, command):
        """Commit UNKNOWN before any independently authorized OS launch."""
        return self._operate(command, "reserve")

    def mark_terminal_data_only(self, command):
        """Record independently verified OS completion, never allow reuse."""
        return self._operate(command, "terminal")

    def close(self):
        """Close only; no reset, refund or alteration of committed records."""
        db, self._db = self._db, None
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass
