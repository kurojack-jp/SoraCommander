"""Disabled B01 service persistence candidate; injected connections ONLY.

No connect, path, provisioning, service entrypoint, transport or spawn exists.
SQL is intended for an independently provisioned protected SQLite database.
Fake-connection verification is NOT SQLite/OS acceptance. Every receipt is data.
"""
from dataclasses import dataclass, field
from hashlib import sha256
from threading import Lock, get_ident
from time import monotonic_ns
from types import MappingProxyType

from .offline_b01_full_binding import (
    ContractError, decode_wire_request, _hex, _HEX32,
)

APPLICATION_ID = 0x53434e53
SCHEMA_VERSION = 1
# Review/provisioning specification only: these statements are NEVER executed.
SCHEMA = {
    "registration": "CREATE TABLE registration (singleton INTEGER PRIMARY KEY CHECK(singleton=1), installation_id TEXT NOT NULL CHECK(length(installation_id)=32), generation TEXT NOT NULL CHECK(length(generation)=32), generation_number INTEGER NOT NULL CHECK(generation_number>0), high_water INTEGER NOT NULL CHECK(high_water=generation_number)) STRICT",
    "spent": "CREATE TABLE spent (installation_id TEXT NOT NULL CHECK(length(installation_id)=32), generation TEXT NOT NULL CHECK(length(generation)=32), request_id TEXT NOT NULL CHECK(length(request_id)=32), operation_id TEXT NOT NULL CHECK(length(operation_id)=32), challenge TEXT NOT NULL CHECK(length(challenge)=64), request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64), request_bytes BLOB NOT NULL CHECK(length(request_bytes) BETWEEN 1 AND 8192), PRIMARY KEY(installation_id,generation,request_id), UNIQUE(installation_id,generation,operation_id), UNIQUE(installation_id,generation,challenge), FOREIGN KEY(installation_id,generation) REFERENCES registration(installation_id,generation)) WITHOUT ROWID, STRICT",
    "registration_key": "CREATE UNIQUE INDEX registration_key ON registration(installation_id,generation)",
}
for _table in ("registration", "spent"):
    for _action in ("UPDATE", "DELETE"):
        _name = _table + "_no_" + _action.lower()
        SCHEMA[_name] = (f"CREATE TRIGGER {_name} BEFORE {_action} ON {_table} "
                         "BEGIN SELECT RAISE(ABORT,'immutable ledger'); END")
SCHEMA_RECORDS = tuple(sorted(SCHEMA.items()))
SCHEMA = MappingProxyType(SCHEMA)
ANCHOR_SQL = "SELECT installation_id,generation,generation_number,high_water FROM registration WHERE singleton=1"
LOOKUP_SQL = "SELECT request_sha256,request_bytes FROM spent WHERE installation_id=? AND generation=? AND request_id=?"
REUSE_SQL = "SELECT request_id FROM spent WHERE installation_id=? AND generation=? AND (operation_id=? OR challenge=?) LIMIT 1"
INSERT_SQL = "INSERT INTO spent(installation_id,generation,request_id,operation_id,challenge,request_sha256,request_bytes) VALUES(?,?,?,?,?,?,?)"


@dataclass(frozen=True, slots=True)
class GenerationFenceCandidate:
    installation_id: str
    generation: str
    generation_number: int

    def __post_init__(self):
        for value in (self.installation_id, self.generation):
            _hex(value, _HEX32, "registration identity")
        if type(self.generation_number) is not int or not 1 <= self.generation_number < 2**63:
            raise ContractError("invalid generation high-water")


@dataclass(frozen=True, slots=True)
class SpentObservation:
    status: str
    request_bytes: bytes
    request_sha256: str
    generation_number: int
    authority: str = field(default="NONE_DATA_ONLY", init=False)
    admission: str = field(default="UNAVAILABLE", init=False)
    acceptance: str = field(default="NOT_OS_ACCEPTED", init=False)


class ServiceSpentLedgerCandidate:
    """Sole-thread, non-reentrant bounded transaction candidate, default OFF.

    _connection must be exclusively owned by the future service. Injection does
    not prove sole writer, protected storage, anchor provenance or durability.
    Commit ambiguity/deadline/DB failure latches this adapter closed. Local
    request/op/challenge reservations are never refunded. Recovery only reads;
    ABSENT/SPENT/UNKNOWN can never produce a fresh permission or replay.
    """
    def __init__(self, fence, *, _connection=None, _clock_ms=None, max_attempts=65536):
        if type(fence) is not GenerationFenceCandidate:
            raise ContractError("generation fence required")
        fence.__post_init__()
        if type(max_attempts) is not int or not 1 <= max_attempts <= 65536:
            raise ContractError("bounded retention required")
        self._fence, self._db = fence, _connection
        self._clock = _clock_ms or (lambda: monotonic_ns() // 1_000_000)
        self._owner, self._slot = get_ident(), Lock()
        self._requests, self._ops, self._challenges = set(), set(), set()
        self._halted, self._last_tick = False, -1
        self._maximum = max_attempts

    def _tick(self, deadline=None):
        now = self._clock()
        if (type(now) is not int or not 0 <= now < 2**63 or now < self._last_tick
                or deadline is not None and now >= deadline):
            raise ContractError("deadline or monotonic clock invalid")
        self._last_tick = now
        return now

    def _validate_database(self, deadline):
        db = self._db
        for sql, expected in (("PRAGMA application_id", APPLICATION_ID),
                              ("PRAGMA user_version", SCHEMA_VERSION),
                              ("PRAGMA synchronous", 2),
                              ("PRAGMA busy_timeout", 0),
                              ("PRAGMA foreign_keys", 1)):
            row = db.execute(sql).fetchone()
            if type(row) is not tuple or len(row) != 1 or type(row[0]) is not int or row[0] != expected:
                raise ContractError("database policy mismatch")
            self._tick(deadline)
        # DELETE + FULL only. WAL checkpoint/sidecar acceptance is unresolved.
        if db.execute("PRAGMA journal_mode").fetchone() != ("delete",):
            raise ContractError("journal mode not accepted")
        rows = db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY name").fetchmany(len(SCHEMA_RECORDS) + 1)
        if type(rows) is not list or tuple(rows) != SCHEMA_RECORDS:
            raise ContractError("exact provisioned schema required")
        fence = self._fence
        row = db.execute(ANCHOR_SQL).fetchone()
        expected = (fence.installation_id, fence.generation,
                    fence.generation_number, fence.generation_number)
        if (type(row) is not tuple or len(row) != 4
                or any(type(a) is not type(b) or a != b for a, b in zip(row, expected))):
            raise ContractError("missing/stale/future/conflicting registration")
        self._tick(deadline)

    def observe_data_only(self, raw, *, timeout_ms=1000):
        req = decode_wire_request(raw)
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 5000:
            raise ContractError("invalid transaction deadline")
        digest = sha256(raw).hexdigest()
        result = lambda status: SpentObservation(status, raw, digest, self._fence.generation_number)
        if self._db is None:
            return result("NOT_ENABLED")
        if get_ident() != self._owner or not self._slot.acquire(blocking=False):
            return result("DATA_DENIED_BUSY_OR_WRONG_THREAD")
        began = False
        try:
            if self._halted:
                return result("OUTCOME_UNKNOWN")
            if (req["installation_id"], req["generation"]) != (
                    self._fence.installation_id, self._fence.generation):
                return result("DATA_DENIED_GENERATION")
            consume = req["action"] == "CONSUME"
            key = req["request_id"]
            if key not in self._requests and len(self._requests) >= self._maximum:
                return result("DATA_DENIED_REUSE_OR_FULL")
            if consume:
                if key in self._requests:
                    return result("DATA_TERMINAL")
                if (len(self._requests) >= self._maximum or req["operation_id"] in self._ops
                        or req["challenge"] in self._challenges):
                    return result("DATA_DENIED_REUSE_OR_FULL")
                # Reserve BEFORE clock, connection callbacks, BEGIN and all SQL.
                self._requests.add(key)
                self._ops.add(req["operation_id"])
                self._challenges.add(req["challenge"])
            else:
                # QUERY is recovery only. Even ABSENT cannot later upgrade
                # this request to a fresh consume in this adapter lifetime.
                self._requests.add(key)
            deadline = self._tick() + timeout_ms
            if self._db.isolation_level is not None or self._db.in_transaction is not False:
                raise ContractError("exclusive autocommit connection required")
            # Validate timeout BEFORE BEGIN so preconfigured busy waits cannot
            # silently make lock acquisition unbounded. Never set policy here.
            busy = self._db.execute("PRAGMA busy_timeout").fetchone()
            if type(busy) is not tuple or len(busy) != 1 or type(busy[0]) is not int or busy[0] != 0:
                raise ContractError("zero busy timeout required")
            self._tick(deadline)
            # BEGIN may have taken effect before an injected/driver exception.
            began = True
            self._db.execute("BEGIN IMMEDIATE")
            self._tick(deadline)
            self._validate_database(deadline)
            params = (req["installation_id"], req["generation"], key)
            row = self._db.execute(LOOKUP_SQL, params).fetchone()
            # QUERY differs from CONSUME only in action. Compare canonical
            # consume bytes, not QUERY bytes, for exact stored binding recovery.
            from .offline_b01_full_binding import _canonical, MAX_WIRE
            consume_raw = _canonical({**req, "action": "CONSUME"}, MAX_WIRE)
            stored = (sha256(consume_raw).hexdigest(), consume_raw)
            if row is not None:
                if type(row) is not tuple or len(row) != 2 or type(row[0]) is not str or type(row[1]) is not bytes:
                    raise ContractError("malformed stored binding")
                status = "DATA_SPENT" if row == stored else "DATA_CONFLICT"
            elif not consume:
                status = "DATA_ABSENT"
            elif self._db.execute(REUSE_SQL, (params[0], params[1], req["operation_id"], req["challenge"])).fetchone() is not None:
                status = "DATA_CONFLICT"
            else:
                count = self._db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM spent LIMIT ?)", (self._maximum + 1,)).fetchone()
                if (type(count) is not tuple or len(count) != 1 or type(count[0]) is not int
                        or not 0 <= count[0] < self._maximum):
                    raise ContractError("ledger full or malformed count")
                self._tick(deadline)
                cursor = self._db.execute(INSERT_SQL, (*params, req["operation_id"], req["challenge"], digest, raw))
                if type(cursor.rowcount) is not int or cursor.rowcount != 1:
                    raise ContractError("insert not exactly one row")
                status = "DATA_COMMIT_OBSERVED"
            self._tick(deadline)
            self._db.commit()
            began = False
            self._tick(deadline)
            # No success observation can be constructed before COMMIT ACK.
            return result(status)
        except BaseException as exc:
            self._halted = True
            if began:
                try:
                    self._db.rollback()
                except BaseException:
                    pass  # Remain latched; never claim rollback succeeded.
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return result("OUTCOME_UNKNOWN")
        finally:
            self._slot.release()
