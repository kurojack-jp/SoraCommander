"""Untrusted, immutable DEV job DATA ONLY. Never authorization or code trust.

No live imports, filesystem, clocks, callbacks, process launch or Owner evidence.
Byte comparisons below concern supplied synthetic bytes, never running images.
Serial replay/attempt bookkeeping is defeated by discarding or forking state.
"""

from dataclasses import dataclass
import hashlib
import json
import re


# This checkout's root, from this file's own path. Text only: no import, no file access.
REPO_ROOT = __file__.rsplit("\\", 3)[0]
REPO_ID = "sora-commander-next-dev"
# Proposed future protected runner location; existence/trust is NOT asserted.
RUNNER = r"C:\SoraCommanderProtected\DevJob\sc-dev-job-runner.exe"
MAX_WIRE = 16384
MAX_BYTES = 16 * 1024 * 1024
MAX_ATTEMPTS = 4
MAX_TTL_MS = 600000
_MAX_TIME = 2**63 - 1
_DOMAIN = b"SoraCommanderNext/offline-dev-job-scope/v1\0"
_FIELDS = frozenset({
    "schema_version", "operation", "job_id", "repo_id", "repo_root",
    "task_file", "task_sha256", "runner_executable", "runner_sha256",
    "allowed_source_directories", "write_files", "focused_test_files",
    "command", "effects", "environment", "runtime_epoch", "clock_epoch",
    "created_at_ms", "expires_at_ms", "max_attempts",
})
_COMMAND = frozenset({"argv", "cwd", "background", "timeout_ms", "output_limit"})
_EFFECTS = frozenset({"network", "child_processes", "shell", "elevation", "writes"})
_ENVIRONMENT = frozenset({"mode", "block_sha256"})
_SOURCE = r"src\sora_commander_next"
_TESTS = "tests"


def _fields(data, keys):
    if type(data) is not dict or data.keys() != keys:
        raise ValueError("missing, unknown or invalid fields")


def _int(value, low=0, high=_MAX_TIME):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("bounded integer required")


def _hex(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("lowercase SHA-256 or identifier required")


def _exact(value, expected):
    if type(value) is not type(expected) or value != expected:
        raise ValueError("unsupported literal or scope")


def _files(values, *, tests_only=False):
    # Closed relative ASCII grammar: no aliases, ADS, wildcards, separators
    # inside names, dot segments, hidden files, or repo-wide write permission.
    if type(values) is not list or not 1 <= len(values) <= 16:
        raise ValueError("bounded explicit file list required")
    for value in values:
        if type(value) is not str or len(value) > 160:
            raise ValueError("invalid file")
        test = re.fullmatch(r"tests\\test_[a-z][a-z0-9_]{0,79}\.py", value)
        source = re.fullmatch(r"src\\sora_commander_next\\[a-z][a-z0-9_]{0,79}\.py", value)
        if not test and (tests_only or not source):
            raise ValueError("only explicit source/test Python files supported")
        stem = value.rsplit("\\", 1)[1][:-3].upper()
        if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
            raise ValueError("reserved filename")
    if values != sorted(set(values)):
        raise ValueError("sorted unique exact files required")


def _validate(data):
    _fields(data, _FIELDS)
    _exact(data["schema_version"], 1)
    _exact(data["operation"], "reviewed_patch_and_focused_checks_v1")
    _hex(data["job_id"])
    _exact(data["repo_id"], REPO_ID)
    _exact(data["repo_root"], REPO_ROOT)
    task_file = data["task_file"]
    prefix = REPO_ROOT + "\\dev_tasks\\"
    if (type(task_file) is not str or not task_file.startswith(prefix)
            or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}\.json", task_file[len(prefix):]) is None):
        raise ValueError("exact repo task-file path required")
    if task_file[len(prefix):-5].upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
            r"(?:com|lpt)[1-9]", task_file[len(prefix):-5]):
        raise ValueError("reserved task-file name")
    _hex(data["task_sha256"])
    _exact(data["runner_executable"], RUNNER)
    _hex(data["runner_sha256"])
    dirs = data["allowed_source_directories"]
    if (type(dirs) is not list or not 1 <= len(dirs) <= 2
            or any(type(d) is not str or d not in {_SOURCE, _TESTS} for d in dirs)
            or dirs != sorted(set(dirs))):
        raise ValueError("explicit canonical source directories required")
    _files(data["write_files"])
    _files(data["focused_test_files"], tests_only=True)
    # Directory declarations are ceilings, never recursive write grants.
    for value in data["write_files"] + data["focused_test_files"]:
        if value.rsplit("\\", 1)[0] not in dirs:
            raise ValueError("file outside declared source directories")
    command = data["command"]
    _fields(command, _COMMAND)
    expected_argv = ["--offline-reviewed-task", task_file, "--task-sha256", data["task_sha256"]]
    if (type(command["argv"]) is not list
            or any(type(a) is not str for a in command["argv"])
            or command["argv"] != expected_argv):
        raise ValueError("only the fixed dedicated runner protocol is represented")
    _exact(command["cwd"], REPO_ROOT)
    _exact(command["background"], True)
    _int(command["timeout_ms"], 1, 300000)
    _int(command["output_limit"], 128, 65536)
    effects = data["effects"]
    _fields(effects, _EFFECTS)
    for key in ("network", "child_processes"):
        _exact(effects[key], "deny")
    _exact(effects["shell"], False)
    _exact(effects["elevation"], False)
    _exact(effects["writes"], "listed_files_only")
    env = data["environment"]
    _fields(env, _ENVIRONMENT)
    _exact(env["mode"], "asserted_complete_block_sha256")
    _hex(env["block_sha256"])
    for key in ("runtime_epoch", "clock_epoch"):
        _hex(data[key])
    _int(data["created_at_ms"])
    _int(data["expires_at_ms"])
    ttl = data["expires_at_ms"] - data["created_at_ms"]
    if not command["timeout_ms"] <= ttl <= MAX_TTL_MS:
        raise ValueError("timeout must fit bounded original TTL")
    _int(data["max_attempts"], 1, MAX_ATTEMPTS)


def _json(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class DevJobScope:
    """One immutable canonical contract. 'reviewed' is a claim, not consent."""

    canonical_json: str

    def __post_init__(self):
        if type(self.canonical_json) is not str or len(self.canonical_json) > MAX_WIRE:
            raise ValueError("bounded canonical JSON required")
        try:
            data = json.loads(self.canonical_json, object_pairs_hook=_pairs)
            _validate(data)
            if _json(data) != self.canonical_json:
                raise ValueError("noncanonical JSON")
        except (RecursionError, OverflowError) as exc:
            raise ValueError("invalid JSON nesting/number") from exc

    @property
    def digest(self):
        return hashlib.sha256(_DOMAIN + self.canonical_json.encode("ascii")).hexdigest()

    def to_data(self):
        return json.loads(self.canonical_json)


def freeze(data):
    """Strict input, no defaults or normalization; copies all mutable data."""
    _validate(data)
    return DevJobScope(_json(data))


@dataclass(frozen=True, slots=True)
class AttemptLedger:
    """Serial simulation for ONE scope only; caller-owned and unauthenticated."""

    scope_digest: str
    dispatch_ids: tuple[str, ...] = ()
    last_seen_ms: int = 0
    sealed: bool = False

    def __post_init__(self):
        _hex(self.scope_digest)
        _int(self.last_seen_ms)
        if type(self.sealed) is not bool or type(self.dispatch_ids) is not tuple:
            raise ValueError("immutable ledger required")
        if len(self.dispatch_ids) > MAX_ATTEMPTS:
            raise ValueError("ledger capacity exceeded")
        for value in self.dispatch_ids:
            _hex(value)
        if len(set(self.dispatch_ids)) != len(self.dispatch_ids):
            raise ValueError("duplicate ledger IDs")


@dataclass(frozen=True, slots=True)
class Comparison:
    outcome: str
    reason: str
    ledger: AttemptLedger


def compare_attempt(scope, current, *, task_bytes, runner_bytes, dispatch_id, now_ms, ledger):
    """Compare supplied data, debit BEFORE any imagined work; never run work.

    Thread the returned ledger on EVERY outcome. All valid denials seal this
    scope permanently in the serial model, including rollback/replay/mismatch.
    Malformed inputs raise ValueError; future callers must fail closed on errors.
    Successful attempts never imply completed tests, an approved job or a refund.
    """
    if type(scope) is not DevJobScope or type(current) is not DevJobScope:
        raise ValueError("explicit data scopes required")
    DevJobScope(scope.canonical_json)
    DevJobScope(current.canonical_json)
    if type(ledger) is not AttemptLedger:
        raise ValueError("explicit synthetic ledger required")
    ledger.__post_init__()
    _hex(dispatch_id)
    _int(now_ms)
    for value in (task_bytes, runner_bytes):
        if type(value) is not bytes or not 1 <= len(value) <= MAX_BYTES:
            raise ValueError("bounded exact bytes required")
    data = scope.to_data()
    reason = "matching_asserted_data"
    if ledger.sealed:
        reason = "sealed"
    elif ledger.scope_digest != scope.digest:
        reason = "ledger_scope_mismatch"
    elif dispatch_id in ledger.dispatch_ids:
        reason = "replayed_dispatch"
    elif now_ms < ledger.last_seen_ms:
        reason = "clock_rollback"
    elif not data["created_at_ms"] <= now_ms < data["expires_at_ms"]:
        reason = "outside_original_ttl"
    elif data["command"]["timeout_ms"] > data["expires_at_ms"] - now_ms:
        reason = "insufficient_remaining_ttl"
    elif len(ledger.dispatch_ids) >= data["max_attempts"]:
        reason = "attempt_budget_exhausted"
    elif current.canonical_json != scope.canonical_json:
        reason = "scope_mismatch"
    elif hashlib.sha256(task_bytes).hexdigest() != data["task_sha256"]:
        reason = "task_bytes_mismatch"
    elif hashlib.sha256(runner_bytes).hexdigest() != data["runner_sha256"]:
        reason = "runner_bytes_mismatch"
    denied = reason != "matching_asserted_data"
    ids = ledger.dispatch_ids
    # No eviction, refund or retries of an ID, even after an unknown outcome.
    if not denied:
        ids += (dispatch_id,)
    next_ledger = AttemptLedger(ledger.scope_digest, ids,
                                max(now_ms, ledger.last_seen_ms), ledger.sealed or denied)
    return Comparison("DENY" if denied else "DEV_JOB_DATA_ONLY", reason, next_ledger)
