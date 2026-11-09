"""U2-d OFFLINE ONLY temporary-fixture SQLite receipt/consume simulator.

This is not the protected ledger service. Caller-controlled marker/fixture paths,
Owner/session fields, publisher IDs and requests have ZERO authority. Never use
its results for Broker COMMIT-before-spawn, Owner permission or an OS job.
No network/pipe/service/API registration or subprocess execution occurs here.
"""
from pathlib import Path
from threading import RLock
import os
import sqlite3
import stat

from .offline_b01_full_binding import (
    ContractError, _HEX32, _hex, decode_wire_request,
)

FIXTURE_FILENAME = "u2d-ledger-dev-fixture.sqlite3"
MARKER_FILENAME = ".scn-u2d-data-only-fixture"
MARKER_BYTES = b"SCN-U2D-ISOLATED-FIXTURE-NO-AUTHORITY\n"
APPLICATION_ID = 0x53434E44
SCHEMA_VERSION = 1
MAX_ATTEMPTS = 100000
MAX_PAGES = 65536
SCHEMA = """
CREATE TABLE registration (
    id INTEGER PRIMARY KEY CHECK(id=1),
    installation_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    attempts_limit INTEGER NOT NULL CHECK(attempts_limit BETWEEN 1 AND 100000)
) STRICT;
CREATE TABLE attempts (
    installation_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    request_id TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    broker_instance_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    challenge TEXT NOT NULL,
    owner_sid TEXT NOT NULL,
    logon_luid_low INTEGER NOT NULL,
    logon_luid_high INTEGER NOT NULL,
    wts_session INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status='SPENT'),
    PRIMARY KEY(installation_id,generation,request_id),
    UNIQUE(installation_id,generation,operation_id),
    UNIQUE(installation_id,generation,challenge)
) WITHOUT ROWID, STRICT;
CREATE TRIGGER prevent_update BEFORE UPDATE ON attempts
BEGIN SELECT RAISE(ABORT,'attempt row is immutable'); END;
CREATE TRIGGER prevent_delete BEFORE DELETE ON attempts
BEGIN SELECT RAISE(ABORT,'attempt row cannot be deleted'); END;
"""


class FixtureLedgerError(ContractError):
    """Untrusted temporary ledger not usable (and never a grant)."""


def _normal(path, directory=False):
    try:
        info = path.lstat()
    except OSError as exc:
        raise FixtureLedgerError("fixture path is unavailable") from exc
    if (getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            or path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())):
        raise FixtureLedgerError("reparse/alias paths forbidden")
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise FixtureLedgerError("fixture directory required")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise FixtureLedgerError("ordinary single-link fixture required")
    return info


def _root(value):
    if type(value) is not str or not value:
        raise FixtureLedgerError("explicit fixture root required")
    root = Path(value)
    if not root.is_absolute():
        raise FixtureLedgerError("absolute fixture root required")
    for part in (root, *root.parents):
        _normal(part, directory=True)
    marker = root / MARKER_FILENAME
    _normal(marker)
    try:
        if marker.read_bytes() != MARKER_BYTES:
            raise FixtureLedgerError("wrong development marker")
    except OSError as exc:
        raise FixtureLedgerError("fixture marker unavailable") from exc
    # Caller-managed fixture marker is not an administrative trust anchor.
    return root


def _path(root, *, provision):
    root = _root(root)
    path = root / FIXTURE_FILENAME
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        # SQLite DELETE mode may need to recover a real rollback journal
        # after a crash, but a forged linked sidecar is never accepted.
        if provision or suffix != "-journal":
            raise FixtureLedgerError("unexpected fixture sidecar")
        _normal(sidecar)
    if provision:
        try:
            path.lstat()
        except FileNotFoundError:
            return path
        raise FixtureLedgerError("fixture already exists; no reset")
    info = _normal(path)
    if info.st_size == 0:
        raise FixtureLedgerError("empty fixture cannot be opened")
    return path


def _schema_signature(db):
    return tuple(db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall())


def _reference_signature():
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA)
        return _schema_signature(db)
    finally:
        db.close()


EXPECTED_SCHEMA = _reference_signature()


class OfflineFixtureLedger:
    """Explicitly provisioned, temporary **data-only** SQLite crash model.

    Only a development caller can supply a root with a marker. The marker is
    NOT access control. Do not use with production or protected directories.
    """

    def __init__(self, root, *, create=False, installation_id=None,
                 generation=None, attempts_limit=MAX_ATTEMPTS):
        if type(create) is not bool:
            raise FixtureLedgerError("explicit create bool required")
        _hex(installation_id, _HEX32, "installation")
        _hex(generation, _HEX32, "generation")
        if (type(attempts_limit) is not int or
                not 1 <= attempts_limit <= MAX_ATTEMPTS):
            raise FixtureLedgerError("invalid fixture attempt budget")
        self._lock = RLock()
        self._db = None
        path = _path(root, provision=create)
        try:
            if create:
                # Never initialize from normal open; never overwrite anything.
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(fd)
            before = _normal(path)
            uri = path.as_uri() + "?mode=rw"
            self._db = sqlite3.connect(
                uri, uri=True, timeout=1.0, isolation_level=None,
                check_same_thread=False)
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA trusted_schema=OFF")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute(f"PRAGMA max_page_count={MAX_PAGES}")
            if (self._db.execute("PRAGMA journal_mode").fetchone()[0] != "delete"
                    or self._db.execute("PRAGMA synchronous").fetchone()[0] != 2
                    or self._db.execute("PRAGMA max_page_count").fetchone()[0] > MAX_PAGES):
                raise FixtureLedgerError("storage settings unavailable")
            if create:
                self._db.executescript(SCHEMA)
                self._db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self._db.execute("INSERT INTO registration VALUES(1,?,?,?)",
                                 (installation_id, generation, attempts_limit))
            if (self._db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                    or self._db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
                    or _schema_signature(self._db) != EXPECTED_SCHEMA
                    or self._db.execute("PRAGMA integrity_check").fetchone() != ("ok",)):
                raise FixtureLedgerError("unknown or corrupted fixture ledger")
            reg = self._db.execute(
                "SELECT installation_id,generation,attempts_limit FROM registration").fetchall()
            if len(reg) != 1 or reg[0][0:2] != (installation_id, generation) or (
                    reg[0][2] != attempts_limit):
                raise FixtureLedgerError("fixture registration mismatch")
            after = _normal(path)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise FixtureLedgerError("fixture identity changed during open")
            self.installation_id = installation_id
            self.generation = generation
            self.attempts_limit = attempts_limit
        except BaseException:
            if self._db is not None:
                self._db.close()
            # Keep failed fixture for forensic inspection; no deletion/reset.
            raise

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def _count(self):
        return self._db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]

    @staticmethod
    def _same(row, request):
        return row == (
            request["snapshot_sha256"], request["broker_instance_id"],
            request["operation_id"], request["challenge"], request["owner_sid"],
            request["logon_luid"][0], request["logon_luid"][1],
            request["wts_session"],
        )

    def count_data_only(self):
        with self._lock:
            if self._db is None:
                raise FixtureLedgerError("fixture closed")
            return self._count()

    def apply_data_only(self, raw):
        """Returns DATA_* observations only, NEVER 'COMMITTED_FRESH'.

        A failed/ambiguous COMMIT reports DATA_UNKNOWN without automatically
        reissuing an identical attempt. The caller has NO launch permission.
        """
        request = decode_wire_request(raw)
        if (request["installation_id"] != self.installation_id
                or request["generation"] != self.generation):
            return "DATA_DENIED"
        with self._lock:
            if self._db is None:
                raise FixtureLedgerError("fixture closed")
            started = False
            try:
                # All reads & admission occur under this transaction for
                # inter-process SQLite writer serialization.
                self._db.execute("BEGIN IMMEDIATE")
                started = True
                existing = self._db.execute(
                    "SELECT snapshot_sha256,broker_instance_id,operation_id,challenge,"
                    "owner_sid,logon_luid_low,logon_luid_high,wts_session "
                    "FROM attempts WHERE installation_id=? AND generation=? AND request_id=?",
                    (request["installation_id"], request["generation"], request["request_id"]),
                ).fetchone()
                if existing is not None:
                    result = ("DATA_SPENT" if self._same(existing, request)
                              else "DATA_CONFLICT")
                elif request["action"] == "QUERY":
                    result = "DATA_ABSENT"
                elif self._count() >= self.attempts_limit:
                    result = "DATA_FULL"
                else:
                    self._db.execute(
                        "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,'SPENT')",
                        (request["installation_id"], request["generation"],
                         request["request_id"], request["snapshot_sha256"],
                         request["broker_instance_id"], request["operation_id"],
                         request["challenge"], request["owner_sid"],
                         request["logon_luid"][0], request["logon_luid"][1],
                         request["wts_session"]))
                    result = "DATA_RECORDED_ONCE"
                self._db.commit()
                started = False
                return result
            except sqlite3.IntegrityError:
                if started:
                    self._db.rollback()
                return "DATA_CONFLICT"
            except sqlite3.Error:
                if started:
                    try:
                        self._db.rollback()
                    except sqlite3.Error:
                        pass
                # An ambiguous I/O error has no permission value, even if
                # a row might have reached durable media.
                return "DATA_UNKNOWN"
