"""Pure, UNTRUSTED review-choice -> policy-request SPECIFICATION DATA ONLY.

No Policy/Grant adapter, I/O, clock, identity checks, transport or callbacks.
All events, snapshots and replay history are caller assertions. Even a positive
result cannot prove a human click or authorize anything in a protected runtime.
"""

from dataclasses import dataclass, replace
import json
import re

from .offline_grant_contract import request_digest
from . import offline_owner_choice as choice_ui


_MAX = 2**63 - 1
_CAPACITY = 4096


def _integer(value):
    if type(value) is not int or not 0 <= value <= _MAX:
        raise ValueError("bounded integer required")
    return value


def _digest(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("hex digest or review challenge required")
    return value


@dataclass(frozen=True, slots=True)
class BridgeState:
    review_id: str
    choice: choice_ui.ChoiceState
    revision: int = 0
    selection_revision: int | None = None


@dataclass(frozen=True, slots=True)
class BridgeLedger:
    """Thread every returned ledger; no persistence/authenticity is supplied."""

    closed_nonces: frozenset[str] = frozenset()
    closed_reviews: frozenset[str] = frozenset()
    last_seen_ms: int = 0


@dataclass(frozen=True, slots=True)
class PolicyRequestData:
    """Detached immutable DATA; deliberately incompatible with Policy.Grant."""

    review_id: str
    source_digest: str
    request_digest: str
    request_json: str
    expires_at_ms: int
    selection_revision: int | None
    confirmed_at_ms: int


@dataclass(frozen=True, slots=True)
class BridgeResult:
    outcome: str
    reason: str
    state: BridgeState
    ledger: BridgeLedger
    request_data: PolicyRequestData | None = None


def _snapshots(state):
    if type(state) is not BridgeState:
        raise ValueError("explicit bridge state required")
    _digest(state.review_id)
    _integer(state.revision)
    if state.selection_revision is not None:
        _integer(state.selection_revision)
    panel = choice_ui.present(state.choice)  # also validates the saved UI data
    if (state.revision == 0 and (state.selection_revision is not None
                               or state.choice.selected != "once")
            or state.revision > 0 and state.selection_revision != state.revision):
        raise ValueError("fresh selection revision required")
    source = json.loads(state.choice.request_json)
    candidate = json.loads(state.choice.request_json)
    if state.choice.selected == "once":
        candidate.update(mode="once", ttl_seconds=min(candidate["ttl_seconds"], 60))
    digest = request_digest(candidate)
    expiry = candidate["created_at_ms"] + candidate["ttl_seconds"] * 1000
    if panel.request_digest != digest or panel.expires_at_ms != expiry:
        raise ValueError("presentation and candidate disagree")
    return source, candidate, digest, expiry


def open_bridge(payload, *, review_id, now_ms):
    """Open fresh once-default data. Nothing is translated by opening/selecting.

    A reusable source is narrowed, never extended: TTL starts at source creation.
    A fresh review_id must eventually come from a protected runtime, not this API.
    """
    _digest(review_id)
    return BridgeState(review_id, choice_ui.open_review(payload, now_ms=now_ms))


def translate(state, event, context, *, now_ms, ledger):
    """Pure state transition; positive output is POLICY_REQUEST_DATA_ONLY.

    Context has EXACTLY {request: current full source snapshot, unlocked: bool}.
    Each event binds the CURRENT display digest, review_id and revision. Select
    and confirm also carry choice. Confirm must be a separate transition.
    Malformed snapshots/state/context raise ValueError; bad events and stale
    data deny terminally. Successful translation consumes BOTH modes, since a
    reusable request's submission is distinct from future authorized executions.
    """
    source, candidate, digest, expiry = _snapshots(state)
    now_ms = _integer(now_ms)
    if type(ledger) is not BridgeLedger:
        raise ValueError("explicit bridge ledger required")
    _integer(ledger.last_seen_ms)
    for values in (ledger.closed_nonces, ledger.closed_reviews):
        if type(values) is not frozenset or len(values) > _CAPACITY:
            raise ValueError("invalid bounded ledger")
        for value in values:
            _digest(value)
    if (type(context) is not dict or context.keys() != {"request", "unlocked"}
            or type(context["unlocked"]) is not bool):
        raise ValueError("exact current context required")
    current_digest = request_digest(context["request"])
    source_digest = request_digest(source)

    def finish(reason, *, next_state=state, data=None, pending=False):
        if pending:
            next_ledger = replace(ledger, last_seen_ms=max(now_ms, ledger.last_seen_ms))
            return BridgeResult("PENDING", reason, next_state, next_ledger)
        if data is None:
            denied = replace(next_state.choice, outcome="DENY", reason=reason,
                             last_seen_ms=max(now_ms, next_state.choice.last_seen_ms))
            next_state = replace(next_state, choice=denied)
        # Never evict old history. Saturation denies every later attempt.
        nonces, reviews = ledger.closed_nonces, ledger.closed_reviews
        if len(nonces) < _CAPACITY:
            nonces = nonces | {source["nonce"]}
        if len(reviews) < _CAPACITY:
            reviews = reviews | {state.review_id}
        next_ledger = BridgeLedger(nonces, reviews, max(now_ms, ledger.last_seen_ms))
        return BridgeResult("POLICY_REQUEST_DATA_ONLY" if data else "DENY",
                            reason, next_state, next_ledger, data)

    if (len(ledger.closed_nonces) == _CAPACITY
            or len(ledger.closed_reviews) == _CAPACITY):
        return finish("ledger_full")
    if (source["nonce"] in ledger.closed_nonces
            or state.review_id in ledger.closed_reviews
            or state.choice.outcome != "PENDING"):
        return finish("closed_or_duplicate")
    if now_ms < max(ledger.last_seen_ms, state.choice.last_seen_ms):
        return finish("clock_regressed")
    if not candidate["created_at_ms"] <= now_ms < expiry:
        return finish("expired_or_invalid_time")
    if not context["unlocked"]:
        return finish("session_locked")
    if current_digest != source_digest:
        return finish("current_request_changed")
    if type(event) is not dict or type(event.get("action")) is not str:
        return finish("invalid_event")
    action = event["action"]
    keys = {"action", "review_id", "revision", "request_digest"}
    if action in {"select", "confirm"}:
        keys.add("choice")
    if action not in {"select", "confirm", "cancel", "close", "timeout"} or event.keys() != keys:
        return finish("invalid_event")
    if (type(event["review_id"]) is not str or event["review_id"] != state.review_id
            or type(event["revision"]) is not int or event["revision"] != state.revision
            or type(event["request_digest"]) is not str or event["request_digest"] != digest):
        return finish("stale_display_or_review")
    if action in {"cancel", "close", "timeout"}:
        return finish(action)
    if (type(event["choice"]) is not str or event["choice"] not in {"once", "reusable"}
            or action == "confirm" and event["choice"] != state.choice.selected):
        return finish("choice_mismatch")
    if action == "select":
        if state.revision == _MAX:
            return finish("revision_exhausted")
        selected = choice_ui.transition(state.choice,
            {"action": "select", "choice": event["choice"]}, now_ms=now_ms)
        if selected.outcome != "PENDING":
            return finish(selected.reason)
        revision = state.revision + 1
        return finish("selection_data_only", pending=True,
                      next_state=BridgeState(state.review_id, selected, revision, revision))
    if state.choice.selected == "reusable" and state.selection_revision != state.revision:
        return finish("fresh_selection_required")
    confirmed = choice_ui.transition(state.choice,
        {"action": "confirm", "request_digest": digest}, now_ms=now_ms)
    if confirmed.outcome != "CHOICE_DATA_ONLY":
        return finish(confirmed.reason)
    data = PolicyRequestData(state.review_id, source_digest, digest,
                             json.dumps(candidate, sort_keys=True, ensure_ascii=True),
                             expiry, state.selection_revision, now_ms)
    return finish("untrusted_translation_only", next_state=replace(state, choice=confirmed), data=data)
