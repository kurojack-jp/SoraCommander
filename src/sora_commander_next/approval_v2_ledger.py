"""Approval v2 SQLite ledger, DEV/OFFLINE DATA ONLY.

This records claimed consent and reserves dispatch identifiers; it NEVER grants
OS execution, launches a process, or proves Owner identity. A protected local
broker with authenticated Owner UI and safe command admission MUST own any
future live instance. Do not point at production data from development.
"""
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from threading import RLock
from uuid import uuid4
import os
import stat
import sqlite3

from .approval_v2_contract import ConsentRecord, OperationSpec, compare_claim


@dataclass(frozen=True, slots=True)
class LedgerOutcome:
    status: str
    grant_id: str | None = None
    dispatch_id: str | None = None
    count: int = 0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS consent (
    grant_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    spec_sha256 TEXT NOT NULL,
    choice TEXT NOT NULL CHECK(choice IN ('ONCE','TRUSTED','DENY')),
    max_timeout_ms INTEGER NOT NULL,
    max_output_limit INTEGER NOT NULL,
    review_sha256 TEXT NOT NULL,
    owner_evidence_sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
    spent INTEGER NOT NULL DEFAULT 0 CHECK(spent IN (0,1))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_claim
 ON consent(owner_id, spec_sha256) WHERE revoked=0 AND spent=0;
CREATE TABLE IF NOT EXISTS review_used (
    owner_id TEXT NOT NULL,
    owner_evidence_sha256 TEXT NOT NULL,
    PRIMARY KEY(owner_id, owner_evidence_sha256)
);
CREATE TABLE IF NOT EXISTS dispatch (
    request_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL,
    spec_sha256 TEXT NOT NULL,
    dispatch_id TEXT NOT NULL UNIQUE,
    timeout_ms INTEGER NOT NULL CHECK(timeout_ms BETWEEN 10 AND 3600000),
    output_limit INTEGER NOT NULL CHECK(output_limit BETWEEN 128 AND 65536),
    status TEXT NOT NULL CHECK(status IN ('RESERVED','UNKNOWN','TERMINAL')),
    FOREIGN KEY(grant_id) REFERENCES consent(grant_id)
);
CREATE TABLE IF NOT EXISTS launch_attempt (
    request_id TEXT PRIMARY KEY,
    command_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('UNKNOWN','TERMINAL'))
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    grant_id TEXT,
    request_id TEXT,
    spec_sha256 TEXT
);
"""


def _schema_signature(db):
    """Structural consistency check, NOT a tamper-resistant trust proof."""
    return tuple(db.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))


def _expected_schema_signature():
    with closing(sqlite3.connect(":memory:")) as temporary:
        temporary.executescript(_SCHEMA)
        return _schema_signature(temporary)


_EXPECTED_SCHEMA = _expected_schema_signature()


def _local_unlinked_ledger_path(value):
    """Reject aliases before opening an inert development SQLite database.

    This is path hygiene, NOT a same-user tampering defense. Protected ACLs,
    trusted Broker code and ownership must be established separately.
    """
    p = Path(value)
    if not p.is_absolute():
        raise ValueError("absolute local SQLite database path required")
    if os.name == "nt":
        # No UNC/network share, ADS, drive-relative spelling or device path.
        if (len(p.drive) != 2 or p.drive[1:] != ":"
                or ":" in p.name or p.name.endswith((" ", "."))):
            raise ValueError("ordinary local-drive database path required")
    for item in (p, *p.parents):
        if item.is_symlink() or item.is_junction():
            raise ValueError("linked SQLite database or ancestor not allowed")
    if p.exists():
        metadata = p.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("database must be an ordinary unlinked file")
    elif not p.parent.is_dir():
        raise ValueError("database parent directory must already exist")
    return str(p)


class OfflineApprovalLedger:
    """Bounded atomic data store. NOT a trusted authorization component."""

    def __init__(self, path):
        if str(path) == ":memory:":
            location = ":memory:"
        else:
            location = _local_unlinked_ledger_path(path)
        self._lock = RLock()
        self._db = sqlite3.connect(location, isolation_level=None, timeout=5,
                                   check_same_thread=False)
        try:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            # In an untrusted local path this does not guarantee authenticity.
            self._db.executescript(_SCHEMA)
            if self._db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("corrupted approval ledger")
            # A partially migrated or injected trigger/index/table should
            # never silently acquire the shape of a trusted approval ledger.
            # This is a consistency check only; mutable development DB files
            # remain untrusted and cannot authorize a live process.
            if _schema_signature(self._db) != _EXPECTED_SCHEMA:
                raise RuntimeError("invalid or unexpected approval ledger schema")
            if self._db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("invalid approval ledger foreign key")
        except Exception:
            self._db.close()
            raise

    def close(self):
        with self._lock:
            self._db.close()

    def _begin(self):
        self._db.execute("BEGIN IMMEDIATE")

    def _finish(self, action):
        try:
            result = action()
            self._db.commit()
            return result
        except Exception:
            self._db.rollback()
            raise

    def _record(self, action, grant_id, request_id, spec_sha256):
        self._db.execute(
            "INSERT INTO audit(action,grant_id,request_id,spec_sha256) VALUES(?,?,?,?)",
            (action, grant_id, request_id, spec_sha256))

    @staticmethod
    def _request_id(value):
        if type(value) is not str or not 1 <= len(value) <= 128 or "\0" in value:
            raise ValueError("bounded request ID required")
        return value

    def record_claim(self, operation, claim):
        """Import a DATA-ONLY Owner claim. NO proof of consent or permission.

        Live Broker integration MUST NOT call this with MCP-provided claims.
        """
        if type(operation) is not OperationSpec or type(claim) is not ConsentRecord:
            raise TypeError("typed operation and Owner claim required")
        if (claim.revoked or claim.spent or claim.owner_id != operation.owner_id
                or claim.spec_sha256 != operation.digest):
            raise ValueError("unmatched Owner claim")
        if claim.choice != "DENY" and compare_claim(
                claim, operation, timeout_ms=claim.max_timeout_ms,
                output_limit=claim.max_output_limit) != "MATCHING_DATA_ONLY":
            raise ValueError("invalid Owner claim")
        with self._lock:
            self._begin()
            def install():
                # A previously used Owner review cannot mint another claim
                # after revoke, reopen or a lost/replayed response. This is
                # only offline uniqueness; it does not authenticate consent.
                self._db.execute(
                    "INSERT INTO review_used(owner_id,owner_evidence_sha256) "
                    "VALUES(?,?)",
                    (claim.owner_id, claim.owner_evidence_sha256))
                # DENY is a review outcome, not a permanent active command grant.
                if claim.choice == "DENY":
                    self._record("DENIED_DATA_ONLY", claim.approval_id,
                                 None, claim.spec_sha256)
                    return LedgerOutcome("DENIED_DATA_ONLY")
                self._db.execute(
                    "INSERT INTO consent(grant_id,owner_id,spec_sha256,choice,"
                    "max_timeout_ms,max_output_limit,review_sha256,"
                    "owner_evidence_sha256,revision,revoked,spent)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (claim.approval_id, claim.owner_id, claim.spec_sha256,
                     claim.choice, claim.max_timeout_ms, claim.max_output_limit,
                     claim.review_sha256, claim.owner_evidence_sha256,
                     claim.revision, int(claim.revoked), int(claim.spent)))
                self._record("CLAIMED_" + claim.choice, claim.approval_id,
                             None, claim.spec_sha256)
                return LedgerOutcome("CLAIM_RECORDED_DATA_ONLY", claim.approval_id)
            return self._finish(install)

    def _claim(self, row):
        return ConsentRecord(
            approval_id=row[0], owner_id=row[1], spec_sha256=row[2],
            choice=row[3], max_timeout_ms=row[4], max_output_limit=row[5],
            review_sha256=row[6], owner_evidence_sha256=row[7],
            revision=row[8], revoked=bool(row[9]), spent=bool(row[10]))

    def _read_claim(self, operation):
        row = self._db.execute(
            "SELECT grant_id,owner_id,spec_sha256,choice,max_timeout_ms,"
            "max_output_limit,review_sha256,owner_evidence_sha256,revision,"
            "revoked,spent FROM consent WHERE owner_id=? AND spec_sha256=? "
            "AND revoked=0 AND spent=0 ORDER BY revision DESC LIMIT 1",
            (operation.owner_id, operation.digest)).fetchone()
        return self._claim(row) if row else None

    def inspect(self, operation, *, timeout_ms, output_limit):
        if type(operation) is not OperationSpec:
            return LedgerOutcome("DENY")
        with self._lock:
            claim = self._read_claim(operation)
            if claim is None:
                return LedgerOutcome("REVIEW")
            result = compare_claim(claim, operation, timeout_ms=timeout_ms,
                                   output_limit=output_limit)
            return LedgerOutcome(result, claim.approval_id)

    def reserve_data_only(self, operation, *, timeout_ms, output_limit,
                          request_id):
        """Persist a unique request reservation; never return execution rights.

        Retries with the same ID return ALREADY_RESERVED/UNKNOWN, not a second
        reservation. A fresh request can use an existing TRUSTED claim.
        """
        if type(operation) is not OperationSpec:
            return LedgerOutcome("DENY")
        self._request_id(request_id)
        with self._lock:
            self._begin()
            def reserve():
                previous = self._db.execute(
                    "SELECT grant_id,spec_sha256,dispatch_id,status,"
                    "timeout_ms,output_limit FROM dispatch WHERE request_id=?",
                    (request_id,)).fetchone()
                if previous:
                    if (previous[1] != operation.digest
                            or type(timeout_ms) is not int
                            or type(output_limit) is not int
                            or previous[4] != timeout_ms
                            or previous[5] != output_limit):
                        return LedgerOutcome("DENY_DUPLICATE_CONFLICT")
                    return LedgerOutcome("ALREADY_" + previous[3], previous[0], previous[2])
                claim = self._read_claim(operation)
                if claim is None:
                    return LedgerOutcome("REVIEW")
                outcome = compare_claim(claim, operation, timeout_ms=timeout_ms,
                                        output_limit=output_limit)
                if outcome != "MATCHING_DATA_ONLY":
                    return LedgerOutcome(outcome)
                dispatch_id = uuid4().hex
                self._db.execute(
                    "INSERT INTO dispatch(request_id,grant_id,spec_sha256,"
                    "dispatch_id,timeout_ms,output_limit,status)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (request_id, claim.approval_id, operation.digest,
                     dispatch_id, timeout_ms, output_limit, "RESERVED"))
                if claim.choice == "ONCE":
                    self._db.execute(
                        "UPDATE consent SET spent=1 WHERE grant_id=? AND spent=0",
                        (claim.approval_id,))
                self._record("RESERVED_DATA_ONLY", claim.approval_id,
                             request_id, operation.digest)
                return LedgerOutcome("RESERVED_DATA_ONLY", claim.approval_id,
                                     dispatch_id)
            return self._finish(reserve)

    def conclude_data_only(self, request_id, outcome):
        """Terminal DATA receipt. Never auto-refund or re-launch on UNKNOWN."""
        self._request_id(request_id)
        if type(outcome) is not str or outcome not in ("UNKNOWN", "TERMINAL"):
            raise ValueError("terminal receipt required")
        with self._lock:
            self._begin()
            def finish():
                row = self._db.execute(
                    "SELECT grant_id,dispatch_id,status,spec_sha256 FROM dispatch "
                    "WHERE request_id=?", (request_id,)).fetchone()
                if row is None:
                    return LedgerOutcome("NOT_FOUND")
                if row[2] == "RESERVED":
                    self._db.execute("UPDATE dispatch SET status=? "
                                     "WHERE request_id=? AND status='RESERVED'",
                                     (outcome, request_id))
                    self._record(outcome, row[0], request_id, row[3])
                elif row[2] != outcome:
                    return LedgerOutcome("ALREADY_FINALIZED", row[0], row[1])
                return LedgerOutcome(outcome + "_DATA_ONLY", row[0], row[1])
            return self._finish(finish)

    def recover_unresolved_data_only(self):
        """Mark EVERY orphan-candidate RESERVED row UNKNOWN without replay.

        Development DATA ONLY. Call explicitly during an offline cold-recovery
        operation, and ONLY after an external supervisor has independently
        established no old Broker/JobManager instance is still active.
        SQLite cannot prove that precondition, trusted Owner presence, or an
        installed Broker's code integrity. No job is launched or refunded.
        The operation is atomic and idempotent even after interrupted commits.
        """
        with self._lock:
            self._begin()
            def recover():
                rows = self._db.execute(
                    "SELECT request_id,grant_id,spec_sha256 FROM dispatch "
                    "WHERE status='RESERVED' ORDER BY request_id").fetchall()
                if not rows:
                    return LedgerOutcome("RECOVERY_NO_CHANGE_DATA_ONLY", count=0)
                self._db.execute(
                    "UPDATE dispatch SET status='UNKNOWN' WHERE status='RESERVED'")
                for request_id, grant_id, digest in rows:
                    self._record("RECOVERED_UNKNOWN_DATA_ONLY", grant_id,
                                 request_id, digest)
                return LedgerOutcome("RECOVERED_UNKNOWN_DATA_ONLY",
                                     count=len(rows))
            return self._finish(recover)

    @staticmethod
    def _launch_key(command):
        """DEV-only exact request bytes, not an authorization credential."""
        from .commands import Command
        if type(command) is not Command:
            raise TypeError("validated Command required")
        rid = command.request_id
        if (type(rid) is not str or len(rid) != 32
                or any(c not in "0123456789abcdef" for c in rid)):
            raise ValueError("opaque 32hex request ID required")
        raw = json.dumps(
            ("DEV_ATTEMPT_V1", rid, command.executable, command.argv,
             command.cwd, command.timeout, command.output_limit),
            ensure_ascii=True, separators=(",", ":")).encode("ascii")
        return rid, sha256(raw).hexdigest()

    def inspect_launch_attempt_data_only(self, command):
        """Read-only replay evidence. No permission or grant is returned."""
        rid, digest = self._launch_key(command)
        with self._lock:
            row = self._db.execute(
                "SELECT command_sha256,status FROM launch_attempt "
                "WHERE request_id=?", (rid,)).fetchone()
            if row is None:
                return LedgerOutcome("NOT_ATTEMPTED_DATA_ONLY")
            if row[0] != digest:
                return LedgerOutcome("DENY_DUPLICATE_CONFLICT")
            return LedgerOutcome("ALREADY_ATTEMPTED_" + row[1])

    def reserve_launch_attempt_data_only(self, command):
        """Durably consume this exact request ID BEFORE local OS spawn.

        Never decide Owner consent. No caller may treat this record or its
        successful return as an execution grant. Unknown is irrevocable and
        cannot be retried, even when Popen fails before returning a PID.
        """
        rid, digest = self._launch_key(command)
        with self._lock:
            self._begin()
            def reserve():
                row = self._db.execute(
                    "SELECT command_sha256,status FROM launch_attempt "
                    "WHERE request_id=?", (rid,)).fetchone()
                if row is not None:
                    if row[0] != digest:
                        return LedgerOutcome("DENY_DUPLICATE_CONFLICT")
                    return LedgerOutcome("ALREADY_ATTEMPTED_" + row[1])
                self._db.execute(
                    "INSERT INTO launch_attempt(request_id,command_sha256,status) "
                    "VALUES(?,?,'UNKNOWN')", (rid, digest))
                self._record("LAUNCH_ATTEMPT_DATA_ONLY", None, rid, digest)
                return LedgerOutcome("ATTEMPT_RECORDED_DATA_ONLY")
            return self._finish(reserve)

    def conclude_launch_attempt_data_only(self, command):
        """Record a locally verified terminal observation, never create one."""
        rid, digest = self._launch_key(command)
        with self._lock:
            self._begin()
            def conclude():
                row = self._db.execute(
                    "SELECT command_sha256,status FROM launch_attempt "
                    "WHERE request_id=?", (rid,)).fetchone()
                if row is None:
                    return LedgerOutcome("NOT_FOUND")
                if row[0] != digest:
                    return LedgerOutcome("DENY_DUPLICATE_CONFLICT")
                if row[1] == "TERMINAL":
                    return LedgerOutcome("ALREADY_ATTEMPTED_TERMINAL")
                self._db.execute(
                    "UPDATE launch_attempt SET status='TERMINAL' "
                    "WHERE request_id=? AND status='UNKNOWN'", (rid,))
                self._record("LAUNCH_TERMINAL_DATA_ONLY", None, rid, digest)
                return LedgerOutcome("ATTEMPT_TERMINAL_DATA_ONLY")
            return self._finish(conclude)

    def revoke_data_only(self, grant_id, owner_id):
        """Remove future matching claims, never cancel already running work."""
        if type(grant_id) is not str or not grant_id:
            raise ValueError("grant ID required")
        if type(owner_id) is not str or not owner_id:
            raise ValueError("Owner ID required")
        with self._lock:
            self._begin()
            def revoke():
                row = self._db.execute(
                    "SELECT owner_id,spec_sha256,revoked FROM consent "
                    "WHERE grant_id=?", (grant_id,)).fetchone()
                if row is None:
                    return LedgerOutcome("NOT_FOUND")
                if row[0] != owner_id:
                    return LedgerOutcome("DENY_OWNER")
                if row[2]:
                    return LedgerOutcome("ALREADY_REVOKED", grant_id)
                self._db.execute("UPDATE consent SET revoked=1 WHERE grant_id=?",
                                 (grant_id,))
                self._record("REVOKED_DATA_ONLY", grant_id, None, row[1])
                return LedgerOutcome("REVOKED_DATA_ONLY", grant_id)
            return self._finish(revoke)

    def revoke_owner_data_only(self, owner_id):
        """Persist Owner-wide cancellation of all unspent claims (DATA ONLY)."""
        if type(owner_id) is not str or not owner_id:
            raise ValueError("Owner ID required")
        with self._lock:
            self._begin()
            def revoke_all():
                rows = self._db.execute(
                    "SELECT grant_id,spec_sha256 FROM consent "
                    "WHERE owner_id=? AND revoked=0 AND spent=0",
                    (owner_id,)).fetchall()
                self._db.execute("UPDATE consent SET revoked=1 "
                                 "WHERE owner_id=? AND revoked=0 AND spent=0",
                                 (owner_id,))
                for gid, digest in rows:
                    self._record("REVOKED_DATA_ONLY", gid, None, digest)
                return LedgerOutcome("REVOKED_ALL_DATA_ONLY", count=len(rows))
            return self._finish(revoke_all)

    def list_claims_data_only(self, owner_id):
        """Inert Owner-filtered snapshot, without identity authentication."""
        if type(owner_id) is not str or not owner_id:
            raise ValueError("Owner ID required")
        with self._lock:
            rows = self._db.execute(
                "SELECT grant_id,owner_id,spec_sha256,choice,max_timeout_ms,"
                "max_output_limit,review_sha256,owner_evidence_sha256,revision,"
                "revoked,spent FROM consent WHERE owner_id=? ORDER BY rowid",
                (owner_id,)).fetchall()
            return tuple(self._claim(row) for row in rows)

    def audit_count(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
