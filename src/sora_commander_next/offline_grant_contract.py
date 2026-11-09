"""Untrusted, PURE offline review data. Never an authorization gate or token.

No Policy imports, filesystem checks, clock, OS identity, UI, execution or I/O.
Only version_query is represented; even that executable is NOT trusted here.
All context/identity/digest assertions are caller data, not verified evidence.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import PureWindowsPath
import re


MAX_TTL_SECONDS = {"once": 60, "reusable": 600}
_BINDING = frozenset({"owner_sid", "logon_id", "session_id", "session_epoch",
                      "runtime_id", "policy_instance", "policy_generation",
                      "policy_digest"})
_REQUEST = frozenset({"version", "operation", "nonce", "binding", "project_id",
                      "project_root", "cwd", "executable", "executable_digest",
                      "argv", "mode", "created_at_ms", "ttl_seconds"})
_DECISION = frozenset({"request_digest", "binding", "choice", "decided_at_ms"})
_CONTEXT = frozenset({"binding", "project_id", "project_root", "executable_digest",
                      "unlocked"})
_DOMAIN = b"SoraCommanderNext/offline-review/v1\0"


def _fields(value, keys):
    if type(value) is not dict or value.keys() != keys:
        raise ValueError("missing, unknown or invalid fields")


def _integer(value, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid bounded integer")
    return value


def _text(value):
    if (type(value) is not str or not 1 <= len(value) <= 256
            or re.fullmatch(r"[A-Za-z0-9_.:-]+", value) is None):
        raise ValueError("invalid identifier")
    return value


def _digest(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid SHA-256 or nonce")
    return value


def _path(value):
    # Lexical spelling only: no claim about handles, reparse points or identity.
    if type(value) is not str or not 4 <= len(value) <= 240:
        raise ValueError("invalid path")
    if re.fullmatch(r"[A-Z]:\\[A-Za-z0-9_ .\\-]+", value) is None:
        raise ValueError("ambiguous or nonlocal Windows path")
    path = PureWindowsPath(value)
    if str(path) != value or len(path.parts) < 2:
        raise ValueError("noncanonical path or drive root")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)}
    for part in path.parts[1:]:
        if (part.startswith((".", " ")) or part.endswith((".", " "))
                or part.split(".")[0].upper() in reserved):
            raise ValueError("unsafe path segment")
    return value


def _binding(value):
    _fields(value, _BINDING)
    result = {}
    for key in sorted(_BINDING):
        validate = (_digest if key == "policy_digest" else _integer
                    if key in {"session_id", "session_epoch", "policy_generation"}
                    else _text)
        result[key] = validate(value[key])
    return result


def _request(value):
    _fields(value, _REQUEST)
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("unsupported version")
    if type(value["operation"]) is not str or value["operation"] != "version_query":
        raise ValueError("unsupported operation")
    mode = value["mode"]
    if type(mode) is not str or mode not in MAX_TTL_SECONDS:
        raise ValueError("explicit once/reusable mode required")
    argv = value["argv"]
    if (type(argv) is not list or any(type(arg) is not str for arg in argv)
            or argv not in [["--version"], ["-V"]]):
        raise ValueError("only exact version query arguments are represented")
    root, cwd, exe = (_path(value[k]) for k in ("project_root", "cwd", "executable"))
    # Exact cwd avoids recursive grants. The binary may be outside the project.
    if cwd != root or PureWindowsPath(exe).suffix != ".exe":
        raise ValueError("exact project cwd and explicit .exe required")
    name = PureWindowsPath(exe).stem.lower()
    if name in {"cmd", "powershell", "pwsh", "sh", "bash", "env", "sudo", "su",
                "runas", "psexec", "wscript", "cscript", "mshta", "rundll32",
                "regsvr32", "curl", "wget", "ssh", "scp"}:
        raise ValueError("unsafe executable category")
    result = {"version": 1, "operation": "version_query", "mode": mode,
              "nonce": _digest(value["nonce"]), "binding": _binding(value["binding"]),
              "project_id": _text(value["project_id"]), "project_root": root,
              "cwd": cwd, "executable": exe,
              "executable_digest": _digest(value["executable_digest"]),
              "argv": list(argv), "created_at_ms": _integer(value["created_at_ms"]),
              "ttl_seconds": _integer(value["ttl_seconds"], 1, MAX_TTL_SECONDS[mode])}
    _integer(result["created_at_ms"] + result["ttl_seconds"] * 1000)
    return result


def request_digest(payload):
    """Digest of a validated data snapshot; no authenticity or authority implied."""
    snapshot = json.dumps(_request(payload), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode("ascii")
    return hashlib.sha256(_DOMAIN + snapshot).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewState:
    """Explicit simulation state. Callers must thread it; no durable replay store."""

    revoked: frozenset[str] = frozenset()
    consumed: frozenset[str] = frozenset()
    last_seen_ms: int = 0


@dataclass(frozen=True, slots=True)
class ReviewResult:
    outcome: str
    reason: str
    state: ReviewState


def review(payload, decision, context, *, now_ms, state):
    """Return DENY or REVIEWABLE_DATA_ONLY and an immutable next simulation state.

    Positive output is NEVER APPROVE, AUTO, SCOPED, Policy.Grant or a token.
    Malformed input raises ValueError; well-formed stale/denied data returns DENY.
    No persisted state, actual identity validation or authorization is provided.
    """
    if type(state) is not ReviewState:
        raise ValueError("explicit review state required")
    for values in (state.revoked, state.consumed):
        if type(values) is not frozenset or len(values) > 4096:
            raise ValueError("invalid simulation state")
        for value in values:
            _digest(value)
    _integer(state.last_seen_ms)
    now_ms = _integer(now_ms)
    req = _request(payload)
    digest = request_digest(req)
    _fields(decision, _DECISION)
    _digest(decision["request_digest"])
    binding = _binding(decision["binding"])
    decided = _integer(decision["decided_at_ms"])
    choice = decision["choice"]
    if type(choice) is not str or choice not in {"deny", "once", "reusable"}:
        raise ValueError("explicit decision required")
    _fields(context, _CONTEXT)
    current = _binding(context["binding"])
    _text(context["project_id"])
    _path(context["project_root"])
    _digest(context["executable_digest"])
    if type(context["unlocked"]) is not bool:
        raise ValueError("explicit session status required")

    def result(reason, *, consume=False):
        # Negative observations are terminal for this request when state is kept.
        positive = reason == "matching_offline_data"
        next_state = ReviewState(
            state.revoked if positive else state.revoked | {digest},
            state.consumed | {digest} if consume else state.consumed,
            max(now_ms, state.last_seen_ms))
        return ReviewResult("REVIEWABLE_DATA_ONLY" if positive else "DENY", reason, next_state)

    if digest in state.revoked or digest in state.consumed:
        return result("revoked_or_consumed")
    if now_ms < state.last_seen_ms:
        return result("clock_regressed")
    expiry = req["created_at_ms"] + req["ttl_seconds"] * 1000
    if not req["created_at_ms"] <= decided <= now_ms < expiry:
        return result("expired_or_invalid_time")
    if not context["unlocked"]:
        return result("session_locked")
    if req["binding"] != binding or binding != current:
        return result("binding_changed")
    if (decision["request_digest"] != digest or context["project_id"] != req["project_id"]
            or context["project_root"] != req["project_root"]
            or context["executable_digest"] != req["executable_digest"]):
        return result("snapshot_changed")
    if choice == "deny" or choice != req["mode"]:
        return result("denied_or_mode_changed")
    return result("matching_offline_data", consume=choice == "once")
