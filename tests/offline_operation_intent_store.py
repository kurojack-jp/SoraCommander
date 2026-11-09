"""Disposable DEV_ONLY DATA_ONLY persistence; never execution authorization.

Caller must supply an existing, disposable test-created parent directory. No real
Sora database, Owner, grants, commands, tokens or production imports belong here.
Identifiers/bindings and terminal claims are untrusted client data. Distinct IDs
are never merged. SQLite durability depends on honest OS/storage; path checks are
best effort, not a protected boundary against concurrent same-user replacement.
Coherent tamper and snapshot rollback are explicitly unprevented: G2 HOLD.
No cryptographic authority, antirollback, ACL changes or Owner verification.
"""
import os
from pathlib import Path
import re
import sqlite3
import stat


class IntentStoreHeld(ValueError):
    """DATA_ONLY denial; transaction failure does not prove absence of commit."""


SCHEMA = """CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY NOT NULL
        CHECK(typeof(operation_id)='text' AND length(operation_id)=32
              AND operation_id NOT GLOB '*[^0-9a-f]*'),
    command_binding TEXT NOT NULL
        CHECK(typeof(command_binding)='text' AND length(command_binding)=64
              AND command_binding NOT GLOB '*[^0-9a-f]*'),
    request_id TEXT UNIQUE
        CHECK(request_id IS NULL OR (typeof(request_id)='text'
              AND length(request_id)=32 AND request_id NOT GLOB '*[^0-9a-f]*')),
    state TEXT NOT NULL CHECK(state IN
        ('NEW','RESERVED_UNKNOWN','UNKNOWN_HELD','CONFIRMED_TERMINAL')),
    source_version INTEGER NOT NULL CHECK(source_version=1),
    CHECK((state='NEW' AND request_id IS NULL)
          OR (state<>'NEW' AND request_id IS NOT NULL))
)"""
STATUSES = {
    'NEW': 'NEW_INERT_DATA_ONLY',
    'RESERVED_UNKNOWN': 'HOLD_RESERVED_UNKNOWN_DATA_ONLY',
    'UNKNOWN_HELD': 'HOLD_UNKNOWN_DATA_ONLY',
    'CONFIRMED_TERMINAL': 'HOLD_TERMINAL_NO_REUSE_DATA_ONLY',
}
BUSY_MS = 150


def _hex(value, size):
    if type(value) is not str or re.fullmatch('[0-9a-f]{%d}' % size, value) is None:
        raise IntentStoreHeld('HOLD_INVALID_INPUT_DATA_ONLY')


def _path(value, existing):
    if type(value) not in (str, type(Path())):
        raise IntentStoreHeld('HOLD_INVALID_PATH_DATA_ONLY')
    p = Path(value)
    if (not p.is_absolute() or str(p).startswith(('\\\\', '//'))
            or '..' in p.parts or ':' in p.name or p.name in ('', '.', '..')
            or any(part.lower() in {'windows', 'program files', 'programdata',
                                   '.git', '.codex', '.aws'} for part in p.parts)):
        raise IntentStoreHeld('HOLD_UNSAFE_PATH_DATA_ONLY')
    try:
        for ancestor in (*reversed(p.parent.parents), p.parent):
            info = ancestor.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                raise IntentStoreHeld('HOLD_UNSAFE_PARENT_DATA_ONLY')
        if existing or os.path.lexists(p):
            info = p.lstat()
            if (not existing or not stat.S_ISREG(info.st_mode)
                    or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                raise IntentStoreHeld('HOLD_UNSAFE_FILE_DATA_ONLY')
    except OSError:
        raise IntentStoreHeld('HOLD_PATH_UNAVAILABLE_DATA_ONLY') from None
    return p


def _connect(p):
    # mode=rw forbids SQLite's implicit creation/replacement of a missing DB.
    return sqlite3.connect(p.as_uri() + '?mode=rw', uri=True,
                           timeout=BUSY_MS / 1000, isolation_level=None)


def _validate(conn):
    if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise IntentStoreHeld('HOLD_INTEGRITY_DATA_ONLY')
    signature = conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    if (signature != [('table', 'operations', 'operations', SCHEMA)]
            or conn.execute('PRAGMA user_version').fetchone() != (1,)):
        raise IntentStoreHeld('HOLD_SCHEMA_DATA_ONLY')
    # Even constraint-valid rows are observations, never trusted permissions.
    for op, binding, rid, state, version in conn.execute('SELECT * FROM operations'):
        _hex(op, 32)
        _hex(binding, 64)
        if state not in STATUSES or type(version) is not int or version != 1:
            raise IntentStoreHeld('HOLD_ROW_DATA_ONLY')
        if state == 'NEW':
            if rid is not None:
                raise IntentStoreHeld('HOLD_ROW_DATA_ONLY')
        else:
            _hex(rid, 32)


def _configure(conn):
    if conn.execute('PRAGMA journal_mode=DELETE').fetchone() != ('delete',):
        raise IntentStoreHeld('HOLD_JOURNAL_DATA_ONLY')
    conn.execute('PRAGMA synchronous=FULL')
    conn.execute('PRAGMA busy_timeout=%d' % BUSY_MS)
    if conn.execute('PRAGMA synchronous').fetchone() != (2,):
        raise IntentStoreHeld('HOLD_SYNC_DATA_ONLY')


def provision_new_data_only(path):
    """Explicit exclusive creation in caller's disposable test parent only.

    A failed provision may leave an unusable file; never delete/reset/retry it.
    This function supplies no rights and never opens an existing DB for repair.
    """
    p = _path(path, False)
    conn = None
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        conn = _connect(p)
        _configure(conn)
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(SCHEMA)
        conn.execute('PRAGMA user_version=1')
        _validate(conn)
        conn.execute('COMMIT')
    except (OSError, sqlite3.Error, IntentStoreHeld):
        raise IntentStoreHeld('HOLD_PROVISION_UNCERTAIN_DATA_ONLY') from None
    finally:
        if conn is not None:
            conn.close()
    return 'PROVISIONED_DATA_ONLY'


class OfflineOperationIntentStore:
    """Existing-only disposable store. No API result grants an OS attempt."""

    def __init__(self, path):
        self._conn = None
        self._failed = False
        self._path = _path(path, True)
        try:
            self._conn = _connect(self._path)
            self._conn.execute('BEGIN IMMEDIATE')
            _validate(self._conn)
            self._conn.execute('COMMIT')
            _configure(self._conn)
        except (sqlite3.Error, IntentStoreHeld):
            self.close()
            raise IntentStoreHeld('HOLD_OPEN_DATA_ONLY') from None

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def _transaction(self, action):
        if self._failed or self._conn is None:
            raise IntentStoreHeld('HOLD_STORE_UNAVAILABLE_DATA_ONLY')
        try:
            _path(self._path, True)
            self._conn.execute('BEGIN IMMEDIATE')
            _validate(self._conn)
            result = action(self._conn)
            self._conn.execute('COMMIT')
            return result
        except Exception:
            # COMMIT may have happened despite an exception. Do not retry or
            # leave a failed transaction holding locks in another connection.
            self._failed = True
            conn, self._conn = self._conn, None
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            raise IntentStoreHeld('HOLD_TRANSACTION_UNCERTAIN_DATA_ONLY') from None

    @staticmethod
    def _result(row, status=None):
        if row is None:
            return {'status': 'HOLD_MISSING_DATA_ONLY', 'request_id': None,
                    'state': None, 'data_only': True}
        return {'status': status or STATUSES[row[3]], 'request_id': row[2],
                'state': row[3], 'data_only': True}

    @staticmethod
    def _row(conn, op):
        return conn.execute('SELECT * FROM operations WHERE operation_id=?',
                            (op,)).fetchone()

    def record_new_data_only(self, operation_id, command_binding):
        _hex(operation_id, 32)
        _hex(command_binding, 64)
        def action(conn):
            row = self._row(conn, operation_id)
            if row is not None:
                status = ('ALREADY_DATA_ONLY' if row[1] == command_binding
                          else 'HOLD_BINDING_CONFLICT_DATA_ONLY')
                return self._result(row, status)
            conn.execute('INSERT INTO operations VALUES (?,?,NULL,?,1)',
                         (operation_id, command_binding, 'NEW'))
            return self._result(self._row(conn, operation_id))
        return self._transaction(action)

    def reserve_first_attempt_data_only(self, operation_id, command_binding, request_id):
        _hex(operation_id, 32)
        _hex(command_binding, 64)
        _hex(request_id, 32)
        def action(conn):
            row = self._row(conn, operation_id)
            if row is None:
                return self._result(None)
            if row[1] != command_binding:
                return self._result(row, 'HOLD_BINDING_CONFLICT_DATA_ONLY')
            if row[3] != 'NEW':
                return self._result(row)
            if conn.execute('SELECT 1 FROM operations WHERE request_id=?',
                            (request_id,)).fetchone():
                return self._result(row, 'HOLD_REQUEST_USED_DATA_ONLY')
            conn.execute("UPDATE operations SET state='RESERVED_UNKNOWN', "
                         'request_id=? WHERE operation_id=?', (request_id, operation_id))
            return self._result(self._row(conn, operation_id),
                                'RESERVATION_RECORDED_DATA_ONLY')
        return self._transaction(action)

    def _mark(self, operation_id, request_id, command_binding, target):
        _hex(operation_id, 32)
        _hex(request_id, 32)
        _hex(command_binding, 64)
        def action(conn):
            row = self._row(conn, operation_id)
            if row is None:
                return self._result(None)
            if row[1] != command_binding or row[2] != request_id:
                return self._result(row, 'HOLD_IDENTITY_CONFLICT_DATA_ONLY')
            if row[3] in ('RESERVED_UNKNOWN', 'UNKNOWN_HELD'):
                conn.execute('UPDATE operations SET state=? WHERE operation_id=?',
                             (target, operation_id))
            return self._result(self._row(conn, operation_id))
        return self._transaction(action)

    def mark_unknown_data_only(self, operation_id, request_id, command_binding):
        return self._mark(operation_id, request_id, command_binding, 'UNKNOWN_HELD')

    def mark_terminal_data_only(self, operation_id, request_id, command_binding):
        """Store an unverified terminal data claim; never permit reuse."""
        return self._mark(operation_id, request_id, command_binding, 'CONFIRMED_TERMINAL')

    def inspect_data_only(self, operation_id):
        _hex(operation_id, 32)
        return self._transaction(lambda conn: self._result(self._row(conn, operation_id)))
