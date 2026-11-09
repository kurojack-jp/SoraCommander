"""U2-b DATA ONLY command approval binding, service wire and volatile spent model.

No Owner authentication, OS identity verification, approval, file access,
SQLite persistence, protected key, broker service, IPC or process launch.
All input including 'identity' is UNTRUSTED asserted data. This module is
DEV_ONLY and MUST NOT be imported by any production runtime.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import re
import sqlite3
from threading import RLock

DOMAIN = b"sora-commander-next/approval-command/v1\x00"
MAX_SNAPSHOT = 65536
MAX_WIRE = 8192
_MAX_SEQ = (1 << 63) - 1
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SID = re.compile(r"S-1-(?:0|[1-9][0-9]*)(?:-(?:0|[1-9][0-9]*)){1,15}\Z")
_SNAPSHOT_KEYS = frozenset((
    "format_version", "operation", "installation_id", "generation",
    "broker_instance_id", "request_id", "owner_sid", "logon_luid",
    "wts_session", "policy_generation", "release_sequence", "approval_nonce",
    "expires_at_tick_ms", "executable", "argv", "cwd", "task_sha256",
    "execution_profile_sha256", "timeout_ms", "output_limit",
))
_WIRE_KEYS = frozenset((
    "v", "action", "installation_id", "generation", "broker_instance_id",
    "request_id", "operation_id", "owner_sid", "logon_luid", "wts_session",
    "challenge", "snapshot_sha256",
))
_STATUSES = frozenset(("COMMITTED_FRESH", "SPENT", "ABSENT",
                       "CONFLICT", "DENIED", "UNKNOWN"))
_SCHEMA = """
CREATE TABLE attempts (
 installation_id TEXT NOT NULL CHECK(length(installation_id)=32),
 generation TEXT NOT NULL CHECK(length(generation)=32),
 request_id TEXT NOT NULL CHECK(length(request_id)=32),
 snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64),
 broker_instance_id TEXT NOT NULL CHECK(length(broker_instance_id)=32),
 operation_id TEXT NOT NULL CHECK(length(operation_id)=32),
 challenge TEXT NOT NULL CHECK(length(challenge)=64),
 owner_sid TEXT NOT NULL,
 logon_luid_low INTEGER NOT NULL,
 logon_luid_high INTEGER NOT NULL,
 wts_session INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status='SPENT'),
 PRIMARY KEY(installation_id, generation, request_id),
 UNIQUE(installation_id, generation, operation_id),
 UNIQUE(installation_id, generation, challenge)
) WITHOUT ROWID, STRICT;
CREATE TRIGGER attempts_no_delete BEFORE DELETE ON attempts
BEGIN SELECT RAISE(ABORT, 'spent entries cannot be deleted'); END;
CREATE TRIGGER attempts_no_update BEFORE UPDATE ON attempts
BEGIN SELECT RAISE(ABORT, 'spent entries cannot be changed'); END;
"""


class ContractError(ValueError):
    """Untrusted data rejected without granting any execution capability."""


def _exact(value, predicate, message):
    if not predicate(value):
        raise ContractError(message)


def _hex(value, pattern, name):
    _exact(value, lambda x: type(x) is str and pattern.fullmatch(x) is not None,
           "invalid " + name)


def _number(value, low, high, name):
    _exact(value, lambda x: type(x) is int and low <= x <= high,
           "invalid " + name)


def _sid(value):
    _exact(value, lambda x: type(x) is str and len(x.encode("utf-8", errors="surrogatepass")) <= 184
           and _SID.fullmatch(x) is not None, "invalid claimed SID")


def _luid(value):
    _exact(value, lambda x: type(x) is list and len(x) == 2, "invalid LUID")
    for n in value:
        _number(n, 0, 0xFFFFFFFF, "LUID DWORD")


def _text(value, maximum, name):
    _exact(value, lambda x: type(x) is str and "\x00" not in x,
           "invalid " + name)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ContractError("invalid Unicode in " + name) from exc
    _exact(encoded, lambda b: len(b) <= maximum, "oversized " + name)


def _assert_keys(value, expected):
    if type(value) is not dict or set(value) != expected:
        raise ContractError("missing or unknown schema keys")


def _canonical(data, maximum):
    try:
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("ascii")
    except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise ContractError("cannot canonicalize") from exc
    if not 0 < len(raw) <= maximum:
        raise ContractError("canonical data exceeds limit")
    return raw


def _pairs(items):
    result = {}
    for k, v in items:
        if k in result:
            raise ContractError("duplicate JSON key")
        result[k] = v
    return result


def _decode(raw, maximum):
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise ContractError("invalid framed payload bytes")
    try:
        data = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ContractError("invalid JSON constant")))
    except (UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        raise ContractError("invalid JSON payload") from exc
    if _canonical(data, maximum) != raw:
        raise ContractError("noncanonical JSON representation")
    return data


def _windows_local_path(value):
    _text(value, 8192, "path")
    # Purely lexical: no file stat, reparse resolution or trust claim.
    if (len(value) < 4 or value[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            or not value.startswith(":\\", 1) or "/" in value):
        raise ContractError("fixed-drive absolute path required")
    parts = value[3:].split("\\")
    reserved = re.compile(
        r"(?i:CON|PRN|AUX|NUL|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?\Z")
    for part in parts:
        if (not part or part in (".", "..") or part.endswith((" ", "."))
                or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
                or reserved.fullmatch(part) is not None):
            raise ContractError("ambiguous Windows path component")


def _validate_snapshot(data):
    _assert_keys(data, _SNAPSHOT_KEYS)
    _number(data["format_version"], 1, 1, "format")
    _exact(data["operation"], lambda x: type(x) is str and x == "COMMAND",
           "command-only format")
    for key in ("installation_id", "generation", "broker_instance_id", "request_id"):
        _hex(data[key], _HEX32, key)
    for key in ("task_sha256", "execution_profile_sha256", "approval_nonce"):
        _hex(data[key], _HEX64, key)
    _sid(data["owner_sid"])
    _luid(data["logon_luid"])
    _number(data["wts_session"], 1, 0xFFFFFFFF, "interactive session")
    for key in ("policy_generation", "release_sequence", "expires_at_tick_ms"):
        _number(data[key], 0, _MAX_SEQ, key)
    for key in ("executable", "cwd"):
        _windows_local_path(data[key])
    _exact(data["argv"], lambda x: type(x) is list and len(x) <= 32,
           "argv shape")
    for arg in data["argv"]:
        _text(arg, 4096, "argv")
    _number(data["timeout_ms"], 10, 3600000, "timeout")
    _number(data["output_limit"], 128, 65536, "output limit")


@dataclass(frozen=True, slots=True)
class FrozenCommand:
    canonical_bytes: bytes
    sha256: str

    def __post_init__(self):
        if type(self.canonical_bytes) is not bytes or type(self.sha256) is not str:
            raise ContractError("immutable command bytes required")
        data = _decode(self.canonical_bytes, MAX_SNAPSHOT)
        _validate_snapshot(data)
        if sha256(DOMAIN + self.canonical_bytes).hexdigest() != self.sha256:
            raise ContractError("snapshot fingerprint does not match")


def freeze_command(data):
    """Return immutable bytes+hash, but NEVER a validated OS/Owner permission."""
    _validate_snapshot(data)
    raw = _canonical(data, MAX_SNAPSHOT)
    return FrozenCommand(raw, sha256(DOMAIN + raw).hexdigest())


def parse_command(raw):
    data = _decode(raw, MAX_SNAPSHOT)
    _validate_snapshot(data)
    return FrozenCommand(raw, sha256(DOMAIN + raw).hexdigest())


def _validate_wire(data, *, reply):
    _assert_keys(data, _WIRE_KEYS | ({"status"} if reply else set()))
    _number(data["v"], 1, 1, "wire version")
    _exact(data["action"], lambda x: type(x) is str and x in ("CONSUME", "QUERY"),
           "wire action")
    for key in ("installation_id", "generation", "broker_instance_id",
                "request_id", "operation_id"):
        _hex(data[key], _HEX32, key)
    for key in ("challenge", "snapshot_sha256"):
        _hex(data[key], _HEX64, key)
    _sid(data["owner_sid"])
    _luid(data["logon_luid"])
    _number(data["wts_session"], 1, 0xFFFFFFFF, "wire WTS session")
    if reply:
        _exact(data["status"], lambda x: type(x) is str and x in _STATUSES,
               "reply status")
        if ((data["action"] == "QUERY" and data["status"] == "COMMITTED_FRESH")
                or (data["action"] == "CONSUME" and data["status"] == "ABSENT")):
            raise ContractError("incompatible reply/action")


def make_wire_request(snapshot, *, operation_id, action="CONSUME"):
    """Derive correlation fields without command body; no real IPC."""
    if type(snapshot) is not FrozenCommand:
        raise ContractError("frozen command required")
    data = _decode(snapshot.canonical_bytes, MAX_SNAPSHOT)
    _validate_snapshot(data)
    result = {
        "v": 1, "action": action,
        "installation_id": data["installation_id"], "generation": data["generation"],
        "broker_instance_id": data["broker_instance_id"],
        "request_id": data["request_id"], "operation_id": operation_id,
        "owner_sid": data["owner_sid"], "logon_luid": data["logon_luid"],
        "wts_session": data["wts_session"], "challenge": data["approval_nonce"],
        "snapshot_sha256": snapshot.sha256,
    }
    _validate_wire(result, reply=False)
    return _canonical(result, MAX_WIRE)


def decode_wire_request(raw):
    data = _decode(raw, MAX_WIRE)
    _validate_wire(data, reply=False)
    return data


def make_wire_reply(request, status):
    """Data-only echo. COMMITTED_FRESH is never created by this ledger model."""
    _validate_wire(request, reply=False)
    value = {**request, "status": status}
    _validate_wire(value, reply=True)
    return _canonical(value, MAX_WIRE)


def decode_wire_reply(raw, *, expected_request):
    value = _decode(raw, MAX_WIRE)
    _validate_wire(value, reply=True)
    _validate_wire(expected_request, reply=False)
    if any(value[k] != expected_request[k] for k in _WIRE_KEYS):
        raise ContractError("reply binding mismatch")
    return value


class InMemorySpentModel:
    """Volatile SQLite mock; NEVER persist or create a Broker service.

    All labels are caller-asserted. The returned statuses intentionally use
    DATA_ prefixes: they cannot become a wire COMMITTED_FRESH authorization.
    """

    def __init__(self, *, installation_id, generation, max_attempts=100000):
        _hex(installation_id, _HEX32, "installation")
        _hex(generation, _HEX32, "generation")
        _number(max_attempts, 1, 100000, "attempt cap")
        self.installation_id, self.generation = installation_id, generation
        self._lock = RLock()
        self._db = sqlite3.connect(":memory:", isolation_level=None,
                                   check_same_thread=False)
        try:
            self._db.executescript(_SCHEMA)
        except Exception:
            self._db.close()
            raise
        self.max_attempts = max_attempts

    def close(self):
        with self._lock:
            self._db.close()

    def _lookup(self, req):
        return self._db.execute(
            "SELECT snapshot_sha256,broker_instance_id,operation_id,challenge,"
            "owner_sid,logon_luid_low,logon_luid_high,wts_session FROM attempts "
            "WHERE installation_id=? AND generation=? AND request_id=?",
            (req["installation_id"], req["generation"], req["request_id"]),
        ).fetchone()

    def _same(self, row, req):
        return row == (
            req["snapshot_sha256"], req["broker_instance_id"],
            req["operation_id"], req["challenge"], req["owner_sid"],
            req["logon_luid"][0], req["logon_luid"][1], req["wts_session"],
        )

    def apply_data_only(self, raw):
        """Explicitly fake volatile consume/query, never produce spawn grants."""
        req = decode_wire_request(raw)
        if req["installation_id"] != self.installation_id or req["generation"] != self.generation:
            return "DATA_DENIED"
        with self._lock:
            existing = self._lookup(req)
            if req["action"] == "QUERY":
                if existing is None:
                    return "DATA_ABSENT"
                return "DATA_SPENT" if self._same(existing, req) else "DATA_CONFLICT"
            if existing is not None:
                return "DATA_SPENT" if self._same(existing, req) else "DATA_CONFLICT"
            if self._db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] >= self.max_attempts:
                return "DATA_FULL"
            try:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute(
                    "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,'SPENT')",
                    (req["installation_id"], req["generation"], req["request_id"],
                     req["snapshot_sha256"], req["broker_instance_id"],
                     req["operation_id"], req["challenge"], req["owner_sid"],
                     req["logon_luid"][0], req["logon_luid"][1], req["wts_session"]),
                )
                self._db.commit()
            except sqlite3.IntegrityError:
                self._db.rollback()
                # Same operation ID / nonce cannot create a second spent row.
                return "DATA_CONFLICT"
            except BaseException:
                self._db.rollback()
                raise
            return "DATA_RECORDED_ONCE"

    def count_data_only(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
