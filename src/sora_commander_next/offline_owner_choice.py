"""Pure OFFLINE presentation/choice data. Never a grant, token or live decision.

Only offline_grant_contract validates request data. No GUI, clock, I/O,
callbacks, runtime imports or execution adapter. All identity/path/hash claims
are untrusted caller data. Keep this module disconnected from live approval.
"""

from dataclasses import dataclass
import json

from .offline_grant_contract import request_digest


_NOTICE = (
    "OFFLINE ONLY: choice data, no permission or execution. "
    "Identity, executable bytes and paths are NOT verified. "
    "No wildcard, descendant cwd, other argv, new tasks or implied grants."
)


@dataclass(frozen=True, slots=True)
class ChoiceState:
    request_json: str
    selected: str = "once"
    outcome: str = "PENDING"
    reason: str = "awaiting_confirmation"
    last_seen_ms: int = 0


@dataclass(frozen=True, slots=True)
class Presentation:
    details: str
    choices: tuple[tuple[str, str], ...]
    selected: str
    request_digest: str
    expires_at_ms: int
    outcome: str


def _time(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("explicit bounded integer time required")
    return value


def _source(state):
    if (type(state) is not ChoiceState or type(state.request_json) is not str
            or type(state.selected) is not str or state.selected not in {"once", "reusable"}
            or type(state.outcome) is not str
            or state.outcome not in {"PENDING", "DENY", "CHOICE_DATA_ONLY"}
            or type(state.reason) is not str):
        raise ValueError("invalid offline choice state")
    _time(state.last_seen_ms)
    source = json.loads(state.request_json)
    request_digest(source)
    if state.request_json != json.dumps(source, sort_keys=True, ensure_ascii=True):
        raise ValueError("canonical snapshot required")
    if state.selected == "reusable" and source["mode"] != "reusable":
        raise ValueError("repeat not offered by source data")
    return source


def _candidate(state):
    candidate = _source(state)
    if state.selected == "once":
        candidate.update(mode="once", ttl_seconds=min(candidate["ttl_seconds"], 60))
    return candidate


def open_review(payload, *, now_ms):
    """Freeze validated data. Default selection is once; opening grants nothing.

    Malformed requests raise ValueError without yielding a choice. A reusable
    source can be narrowed to once; a once source can never be broadened.
    TTL always starts at the ORIGINAL creation time, never the click time.
    """
    request_digest(payload)
    now_ms = _time(now_ms)
    state = ChoiceState(json.dumps(payload, sort_keys=True, ensure_ascii=True),
                        last_seen_ms=now_ms)
    candidate = _candidate(state)
    if not candidate["created_at_ms"] <= now_ms < _expiry(candidate):
        return _deny(state, "expired_or_invalid_time", now_ms)
    return state


def _expiry(candidate):
    return candidate["created_at_ms"] + candidate["ttl_seconds"] * 1000


def _deny(state, reason, now_ms):
    return ChoiceState(state.request_json, state.selected, "DENY", reason,
                       max(state.last_seen_ms, now_ms))


def present(state):
    """Complete escaped JSON; never concatenate argv into a shell command.

    Expiry is an exclusive caller-clock millisecond value, not wall time.
    Source and selected snapshots are both displayed in full without truncation.
    A future renderer must retain every field and must not interpret markup.
    """
    source, candidate = _source(state), _candidate(state)
    choices = (("once", "Approve once"),)
    if source["mode"] == "reusable":
        choices += (("reusable", f"Repeat exact scope (up to {source['ttl_seconds']} seconds)"),)
    digest = request_digest(candidate)
    details = json.dumps({
        "notice": _NOTICE,
        "source_request": source,
        "selected_request": candidate,
        "selected_choice": state.selected,
        "scope": "Exact executable + digest + ordered argv + project/root/cwd + all binding fields",
        "expires_at_ms_exclusive": _expiry(candidate),
        "clock": "caller-supplied monotonic milliseconds; not authenticated",
        "request_digest": digest,
        "outcome": state.outcome,
        "reason": state.reason,
    }, sort_keys=True, indent=2, ensure_ascii=True)
    return Presentation(details, choices, state.selected, digest,
                        _expiry(candidate), state.outcome)


def transition(state, event, *, now_ms):
    """Pure reducer: select, confirm, cancel, close or timeout.

    Confirm requires the digest of the CURRENT displayed candidate. Unknown or
    missing event fields deny terminally. Positive terminal data is not a grant;
    state retention/serialization cannot supply trusted anti-replay protection.
    """
    source = _source(state)
    now_ms = _time(now_ms)
    if state.outcome != "PENDING":
        return state
    candidate = _candidate(state)
    if now_ms < state.last_seen_ms:
        return _deny(state, "clock_regressed", now_ms)
    if not candidate["created_at_ms"] <= now_ms < _expiry(candidate):
        return _deny(state, "expired_or_invalid_time", now_ms)
    if type(event) is not dict or type(event.get("action")) is not str:
        return _deny(state, "invalid_event", now_ms)
    action = event["action"]
    fields = {"select": {"action", "choice"},
              "confirm": {"action", "request_digest"},
              "cancel": {"action"}, "close": {"action"}, "timeout": {"action"}}
    if action not in fields or event.keys() != fields[action]:
        return _deny(state, "invalid_event", now_ms)
    if action in {"cancel", "close", "timeout"}:
        return _deny(state, action, now_ms)
    if action == "select":
        choice = event["choice"]
        if (type(choice) is not str or choice not in {"once", "reusable"}
                or (choice == "reusable" and source["mode"] != "reusable")):
            return _deny(state, "invalid_choice", now_ms)
        return ChoiceState(state.request_json, choice, last_seen_ms=now_ms)
    if (type(event["request_digest"]) is not str
            or event["request_digest"] != request_digest(candidate)):
        return _deny(state, "display_changed", now_ms)
    return ChoiceState(state.request_json, state.selected, "CHOICE_DATA_ONLY",
                       "explicit_offline_confirmation", now_ms)
