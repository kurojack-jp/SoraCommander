"""DEV_ONLY durable launch record for the Owner Broker (B01, SQLite form).

Every launch commits a row before the OS spawn. A row stays UNKNOWN until this
Broker confirms how the job ended. After a Broker restart:

- a used request_id is refused, and
- the same exact command is refused while a row from an earlier run is UNKNOWN,
  before any Owner window appears, whatever request_id it now carries.

The Owner clears a row only by the local command in owner_broker_main. Nothing on
the wire can. This protects against accidental repeats. It runs with the Owner's
rights, so it does not stop the Owner or Owner-level malware from editing the file.
"""
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from .broker_launch_fence import _local_path, _plain_stat
from .jobs import JobManager, _unknown_command_fingerprint

_APPLICATION_ID = 0x53434C4C
_SCHEMA_VERSION = 1
_TIMEOUT_SECONDS = 2.0
_SCHEMA = (
    """CREATE TABLE meta (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL
) WITHOUT ROWID, STRICT""",
    """CREATE TABLE launches (
    request_id TEXT PRIMARY KEY NOT NULL CHECK(length(request_id)=32),
    command_key TEXT NOT NULL CHECK(length(command_key)=64),
    label TEXT NOT NULL CHECK(length(label)<=128),
    instance TEXT NOT NULL CHECK(length(instance)=32),
    status TEXT NOT NULL CHECK(status IN ('UNKNOWN','TERMINAL','NOT_STARTED','RESOLVED')),
    started_at INTEGER NOT NULL
) WITHOUT ROWID, STRICT""",
    "CREATE INDEX launches_unknown ON launches(command_key, status)",
)
_ROW_ID = re.compile("[0-9a-f]{32}")
# Start replies that prove no child was created.
_NOT_STARTED = frozenset({"BUSY", "DENY_DUPLICATE_UNKNOWN", "DENY_DUPLICATE_EXPIRED",
                          "DENY_DUPLICATE_CONFLICT", "DENY_REPLAY_GUARD_FULL"})
_UNCONFIRMED_JOB_STATES = frozenset({"RUNNING", "CANCELLING", "OUTCOME_UNKNOWN"})


class LaunchLedgerError(RuntimeError):
    """Storage refused. This is always a denial, never permission to launch."""


class LaunchLedger:
    """One SQLite file. Commits use FULL sync with a DELETE journal, not WAL."""

    def __init__(self, path):
        self._lock = threading.RLock()
        self._db = None
        self.instance = secrets.token_hex(16)
        self._path = _local_path(str(path))
        try:
            for parent in reversed(self._path.parents):
                _plain_stat(parent, directory=True)
            info = _plain_stat(self._path, missing=True)
            if info is not None and info.st_size == 0:
                raise LaunchLedgerError("empty ledger file")
            for suffix in ("-wal", "-shm"):
                if _plain_stat(Path(str(self._path) + suffix), missing=True) is not None:
                    raise LaunchLedgerError("unexpected SQLite sidecar")
            self._db = sqlite3.connect(
                self._path.as_uri() + ("?mode=rw" if info is not None else "?mode=rwc"),
                uri=True, isolation_level=None, timeout=_TIMEOUT_SECONDS,
                check_same_thread=False)
            self._db.execute("PRAGMA trusted_schema=OFF")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            if info is None:
                self._create()
            self._check()
            self._salt = bytes.fromhex(self._db.execute(
                "SELECT value FROM meta WHERE key='salt'").fetchone()[0])
        except LaunchLedgerError:
            self.close()
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError) as error:
            self.close()
            raise LaunchLedgerError("launch ledger refused") from error

    def _create(self):
        self._db.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA:
            self._db.execute(statement)
        # A per-file key, so a digest of a command cannot be compared elsewhere.
        self._db.execute("INSERT INTO meta VALUES ('salt', ?)", (secrets.token_hex(32),))
        self._db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
        self._db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        self._db.execute("COMMIT")

    def _check(self):
        db = self._db
        schema = [row[0] for row in db.execute(
            "SELECT sql FROM sqlite_schema ORDER BY rowid").fetchall()]
        if (db.execute("PRAGMA application_id").fetchone() != (_APPLICATION_ID,)
                or db.execute("PRAGMA user_version").fetchone() != (_SCHEMA_VERSION,)
                or db.execute("PRAGMA journal_mode").fetchone() != ("delete",)
                or [text for text in schema if text is not None] != list(_SCHEMA)
                or db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]):
            raise LaunchLedgerError("launch ledger format mismatch")

    def key_for(self, request):
        return hmac.new(self._salt, _unknown_command_fingerprint(request),
                        hashlib.sha256).hexdigest()

    def blocked(self, request):
        """True while an earlier Broker run left this exact command UNKNOWN."""
        key = self.key_for(request)
        with self._lock:
            if self._db is None:
                raise LaunchLedgerError("launch ledger closed")
            return self._db.execute(
                "SELECT 1 FROM launches WHERE command_key=? AND status='UNKNOWN' "
                "AND instance<>? LIMIT 1", (key, self.instance)).fetchone() is not None

    def reserve(self, request):
        """Commit before the spawn. Returns NEW, or the existing row's status."""
        key = self.key_for(request)
        label = Path(request.executable).name[:128]
        with self._lock:
            if self._db is None:
                raise LaunchLedgerError("launch ledger closed")
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT command_key,status,instance FROM launches WHERE request_id=?",
                    (request.request_id,)).fetchone()
                if row is not None:
                    outcome = ("CONFLICT" if row[0] != key
                               else "SAME_RUN" if row[2] == self.instance else "USED")
                elif self._db.execute(
                        "SELECT 1 FROM launches WHERE command_key=? AND status='UNKNOWN' "
                        "AND instance<>? LIMIT 1", (key, self.instance)).fetchone():
                    outcome = "BLOCKED"
                else:
                    self._db.execute(
                        "INSERT INTO launches VALUES (?,?,?,?,'UNKNOWN',?)",
                        (request.request_id, key, label, self.instance, int(time.time())))
                    outcome = "NEW"
                self._db.execute("COMMIT")
                return outcome
            except sqlite3.Error as error:
                self._fail()
                raise LaunchLedgerError("launch ledger write failed") from error

    def finish(self, request_id, status):
        """Only moves a row from UNKNOWN. A failure leaves it UNKNOWN."""
        if status not in ("TERMINAL", "NOT_STARTED"):
            raise ValueError("invalid ledger status")
        with self._lock:
            if self._db is None:
                return False
            try:
                self._db.execute("UPDATE launches SET status=? WHERE request_id=? "
                                 "AND status='UNKNOWN'", (status, request_id))
                return True
            except sqlite3.Error:
                self._fail()
                return False

    def unresolved(self):
        """Rows from other runs. No argv is stored, only the program's name."""
        with self._lock:
            if self._db is None:
                raise LaunchLedgerError("launch ledger closed")
            rows = self._db.execute(
                "SELECT request_id,label,started_at FROM launches "
                "WHERE status='UNKNOWN' AND instance<>? ORDER BY started_at",
                (self.instance,)).fetchall()
        return [{"request_id": row[0], "program": row[1], "started_at": row[2]} for row in rows]

    def resolve_local(self, request_id):
        """Owner's local decision that this attempt needs no further hold."""
        if type(request_id) is not str or _ROW_ID.fullmatch(request_id) is None:
            raise ValueError("invalid request ID")
        with self._lock:
            if self._db is None:
                raise LaunchLedgerError("launch ledger closed")
            cursor = self._db.execute(
                "UPDATE launches SET status='RESOLVED' WHERE request_id=? AND status='UNKNOWN' "
                "AND instance<>?", (request_id, self.instance))
            return cursor.rowcount == 1

    def _fail(self):
        db, self._db = self._db, None
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass

    def close(self):
        with self._lock:
            self._fail()


class DurableJobManager(JobManager):
    """The existing JobManager, with the ledger checked and written first."""

    def __init__(self, *, ledger, **arguments):
        if type(ledger) is not LaunchLedger:
            raise TypeError("launch ledger required")
        self._ledger = ledger
        super().__init__(**arguments)

    def blocked_unknown_command_readonly(self, request):
        # Runs before the Owner window, so a blocked command opens none.
        try:
            if self._ledger.blocked(request):
                return True
        except Exception:
            return "GUARD_UNAVAILABLE"
        return super().blocked_unknown_command_readonly(request)

    def start(self, request):
        try:
            outcome = self._ledger.reserve(request)
        except Exception:
            return {"request_id": request.request_id, "status": "DENY_REPLAY_GUARD_FULL",
                    "error": "ProtectedBrokerLedgerRequired"}
        if outcome == "CONFLICT":
            return {"request_id": request.request_id, "status": "DENY_DUPLICATE_CONFLICT",
                    "error": "RequestIdConflict"}
        if outcome == "USED":
            return {"request_id": request.request_id, "status": "DENY_DUPLICATE_UNKNOWN",
                    "error": "RequestIdUsedInEarlierRun"}
        if outcome == "BLOCKED":
            return {"request_id": request.request_id, "status": "DENY_DUPLICATE_UNKNOWN",
                    "error": "PossibleDuplicateLaunchOutcomeUnknown"}
        # An exception here is an unconfirmed launch, so the row stays UNKNOWN.
        result = super().start(request)
        status = result.get("status") if type(result) is dict else None
        if outcome == "NEW" and status in _NOT_STARTED:
            self._ledger.finish(request.request_id, "NOT_STARTED")
        elif status == "RUNNING" and result.get("reused_session") is not True:
            self._watch(request.request_id, result.get("session_id"))
        return result

    def _watch(self, request_id, session_id):
        job = self._jobs.get(session_id)
        if job is None:
            return  # The row stays UNKNOWN: this run could not confirm the end.

        def record():
            job.done.wait()
            if job.status not in _UNCONFIRMED_JOB_STATES:
                self._ledger.finish(request_id, "TERMINAL")

        threading.Thread(target=record, daemon=True).start()
