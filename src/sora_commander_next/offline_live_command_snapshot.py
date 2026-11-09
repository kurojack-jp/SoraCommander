"""Untrusted OFFLINE snapshot specification. No authorization or live adapter.

All executable, environment, policy and identity evidence is caller ASSERTED.
Lexical checks do not verify bytes, handles, dependencies or Windows identity.
Replay rejection is SERIAL MODEL ONLY; copying/resetting state defeats it.
No live imports, I/O, clock, callbacks, randomness, GUI or command execution.
"""

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import PureWindowsPath
import re


_DOMAIN = b"SoraCommanderNext/offline-live-command-snapshot/v1\0"
MAX_HISTORY = 4096
MAX_WIRE = 32768
_MAX = 2**63 - 1
_FIELDS = frozenset({
    "schema_version", "operation", "nonce", "request_id", "executable",
    "executable_digest", "argv", "cwd", "project_root", "project_id",
    "timeout", "output_limit", "background", "dispatch_mode", "launch_profile",
    "environment", "binding", "approval_mode", "issued_at_ms", "expires_at_ms",
})
_BINDING = frozenset({"owner_sid", "logon_luid", "session_id", "session_epoch",
                      "runtime_id", "clock_epoch", "policy_instance",
                      "policy_generation", "policy_digest"})
_ENV = frozenset({"mode", "block_digest", "policy_id", "policy_generation"})


def _fields(value, fields):
    if type(value) is not dict or value.keys() != fields:
        raise ValueError("missing, unknown or invalid schema fields")


def _integer(value, low=0, high=_MAX):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("bounded integer required")


def _identifier(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) is None:
        raise ValueError("explicit identifier required")


def _hex(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("lowercase SHA-256 or nonce required")


def _literal(value, choices):
    if type(value) is not str or value not in choices:
        raise ValueError("unsupported literal")


def _path(value):
    # Deliberately narrow lexical grammar; never resolve/probe a real path.
    if (type(value) is not str or not 4 <= len(value) <= 240
            or re.fullmatch(r"[A-Z]:\\[A-Za-z0-9_ .\\-]+", value) is None):
        raise ValueError("canonical local Windows path required")
    path = PureWindowsPath(value)
    if str(path) != value or len(path.parts) < 2:
        raise ValueError("noncanonical path")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)}
    for part in path.parts[1:]:
        if (part.startswith((".", " ")) or part.endswith((".", " "))
                or part.split(".")[0].upper() in reserved):
            raise ValueError("ambiguous path segment")


def _validate(data):
    _fields(data, _FIELDS)
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported schema version")
    _literal(data["operation"], {"version_query"})
    _hex(data["nonce"])
    _identifier(data["request_id"])
    _identifier(data["project_id"])
    for key in ("executable", "cwd", "project_root"):
        _path(data[key])
    exe = PureWindowsPath(data["executable"])
    if exe.suffix != ".exe" or data["cwd"] != data["project_root"]:
        raise ValueError("explicit .exe and exact project cwd required")
    if exe.stem.lower() in {
        "cmd", "command", "powershell", "powershell_ise", "pwsh", "sh", "bash",
        "dash", "zsh", "fish", "env", "sudo", "su", "runas", "psexec",
        "wscript", "cscript", "mshta", "rundll32", "regsvr32", "curl", "wget",
        "ssh", "scp", "python", "pythonw", "node", "ruby", "perl", "wsl",
    }:
        raise ValueError("shell/interpreter/network scope unsupported")
    argv = data["argv"]
    if (type(argv) is not list or any(type(a) is not str for a in argv)
            or argv not in [["--version"], ["-V"]]):
        raise ValueError("only exact literal version query argv supported")
    _hex(data["executable_digest"])
    timeout = data["timeout"]
    if (type(timeout) not in (int, float) or not 0.01 <= timeout <= 3600
            or not math.isfinite(timeout)):
        raise ValueError("invalid timeout")
    _integer(data["output_limit"], 128, 65536)
    if type(data["background"]) is not bool:
        raise ValueError("explicit background bool required")
    _literal(data["dispatch_mode"], {"job_start", "job_start_wait"})
    expected = "job_start" if data["background"] else "job_start_wait"
    if data["dispatch_mode"] != expected or not data["background"] and timeout > 30:
        raise ValueError("inconsistent dispatch mode or foreground timeout")
    _literal(data["launch_profile"], {"windows_job_suspended_devnull_pipes_closefds_v1"})
    env = data["environment"]
    _fields(env, _ENV)
    # This is an asserted digest of the COMPLETE future explicit child block.
    # Ambient inheritance/allowlist-only digests are deliberately unsupported.
    _literal(env["mode"], {"protected_frozen_block"})
    _hex(env["block_digest"])
    _identifier(env["policy_id"])
    _integer(env["policy_generation"])
    binding = data["binding"]
    _fields(binding, _BINDING)
    for key in ("owner_sid", "runtime_id", "clock_epoch", "policy_instance"):
        _identifier(binding[key])
    luid = binding["logon_luid"]
    if type(luid) is not list or len(luid) != 2:
        raise ValueError("explicit logon LUID pair required")
    _integer(luid[0], 0, 2**32 - 1)
    _integer(luid[1], -(2**31), 2**31 - 1)
    _integer(binding["session_id"], 1, 2**32 - 1)
    _integer(binding["session_epoch"])
    _integer(binding["policy_generation"])
    _hex(binding["policy_digest"])
    _literal(data["approval_mode"], {"once", "reusable"})
    _integer(data["issued_at_ms"])
    _integer(data["expires_at_ms"])
    ttl = data["expires_at_ms"] - data["issued_at_ms"]
    ceiling = 60000 if data["approval_mode"] == "once" else 600000
    if not 0 < ttl <= ceiling:
        raise ValueError("invalid original exclusive expiry")


def _json(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Canonical immutable DATA ONLY; not executable/identity verification."""

    canonical_json: str

    def __post_init__(self):
        if type(self.canonical_json) is not str or len(self.canonical_json) > MAX_WIRE:
            raise ValueError("bounded canonical JSON string required")
        try:
            data = json.loads(self.canonical_json, object_pairs_hook=_pairs)
        except (RecursionError, OverflowError) as exc:
            raise ValueError("invalid JSON nesting/number") from exc
        _validate(data)
        if self.canonical_json != _json(data):
            raise ValueError("noncanonical JSON")

    @property
    def digest(self):
        return hashlib.sha256(_DOMAIN + self.canonical_json.encode("ascii")).hexdigest()

    def to_data(self):
        """Fresh mutable copy, never a reference into this immutable snapshot."""
        return json.loads(self.canonical_json)


def freeze(data):
    """Reject omissions/unknowns; freeze every field without defaults or I/O."""
    _validate(data)
    return Snapshot(_json(data))


@dataclass(frozen=True, slots=True)
class ReplayLedger:
    nonces: frozenset[str] = frozenset()
    request_ids: frozenset[str] = frozenset()
    last_seen_ms: int = 0
    sealed: bool = False


@dataclass(frozen=True, slots=True)
class Observation:
    outcome: str
    reason: str
    ledger: ReplayLedger


def observe(snapshot, current, *, unlocked, now_ms, ledger):
    """Serial DATA observation, consuming IDs even on negative observations.

    Both snapshots and unlocked/time are untrusted assertions. The returned
    ledger MUST be threaded even on DENY. No authority, receipt, grant or job.
    Both approval modes admit a source snapshot only once in this model;
    reusable authorization/use counting belongs to a separate future design.
    Malformed data raises ValueError; future runtime must invalidate on errors.
    """
    if type(snapshot) is not Snapshot or type(current) is not Snapshot:
        raise ValueError("explicit snapshots required")
    # Revalidate public dataclasses at the boundary; still not authentication.
    Snapshot(snapshot.canonical_json)
    Snapshot(current.canonical_json)
    if type(unlocked) is not bool or type(ledger) is not ReplayLedger:
        raise ValueError("explicit synthetic context and ledger required")
    if type(ledger.sealed) is not bool:
        raise ValueError("invalid ledger seal")
    _integer(now_ms)
    _integer(ledger.last_seen_ms)
    for history, validator in ((ledger.nonces, _hex), (ledger.request_ids, _identifier)):
        if type(history) is not frozenset or len(history) > MAX_HISTORY:
            raise ValueError("invalid bounded replay history")
        for item in history:
            validator(item)
    data = snapshot.to_data()
    nonce, request_id = data["nonce"], data["request_id"]
    if ledger.sealed or len(ledger.nonces) >= MAX_HISTORY or len(ledger.request_ids) >= MAX_HISTORY:
        return Observation("DENY", "sealed_or_capacity", ReplayLedger(
            ledger.nonces, ledger.request_ids, max(now_ms, ledger.last_seen_ms), True))
    next_ledger = ReplayLedger(ledger.nonces | {nonce}, ledger.request_ids | {request_id},
                               max(now_ms, ledger.last_seen_ms))
    reason = "matching_asserted_data"
    if nonce in ledger.nonces or request_id in ledger.request_ids:
        reason = "replayed_nonce_or_request"
    elif now_ms < ledger.last_seen_ms:
        reason = "clock_regressed"
    elif not data["issued_at_ms"] <= now_ms < data["expires_at_ms"]:
        reason = "expired_or_not_yet_issued"
    elif not unlocked:
        reason = "session_locked"
    elif snapshot.canonical_json != current.canonical_json:
        reason = "snapshot_changed"
    return Observation("SNAPSHOT_DATA_ONLY" if reason == "matching_asserted_data" else "DENY",
                       reason, next_ledger)
