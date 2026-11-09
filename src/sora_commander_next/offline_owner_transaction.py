"""SYNTHETIC ONLY: serial, pure bounded-grant transaction simulation.

Not security proof, real grants, a Policy adapter, or persistent authorization.
Caller-asserted identities, policy stamps, times and UI data are NOT trusted.
Thread the returned state: copies/reset/rollback defeat all replay guarantees.
No I/O, live runtime imports, locks, callbacks, dispatch or ambient clock.
"""

from dataclasses import dataclass, replace
import json
import re

from .offline_grant_contract import request_digest
from .offline_owner_bridge import PolicyRequestData


MAX_HISTORY = 4096
MAX_RECORDS = 128
MAX_USES = 16
_MAX = 2**63 - 1


def _number(value):
    if type(value) is not int or not 0 <= value <= _MAX:
        raise ValueError("bounded integer required")
    return value


def _hex(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("synthetic hex identifier required")
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


@dataclass(frozen=True, slots=True)
class PolicyStamp:
    instance: str
    generation: int
    digest: str


def _stamp(value):
    if (type(value) is not PolicyStamp or type(value.instance) is not str
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value.instance) is None):
        raise ValueError("explicit synthetic policy stamp required")
    _number(value.generation)
    _hex(value.digest)
    return value


def _request_stamp(request):
    binding = request["binding"]
    return PolicyStamp(binding["policy_instance"], binding["policy_generation"],
                       binding["policy_digest"])


@dataclass(frozen=True, slots=True)
class PendingIssue:
    tx_id: str
    data: PolicyRequestData
    source_json: str
    uses: int
    base_revision: int


@dataclass(frozen=True, slots=True)
class SyntheticGrant:
    """Inert model record; neither a credential nor a live Policy.Grant."""

    grant_id: str
    source_json: str
    request_json: str
    policy: PolicyStamp
    expires_at_ms: int
    remaining: int


@dataclass(frozen=True, slots=True)
class Simulation:
    policy: PolicyStamp
    revision: int = 0
    pending: tuple[PendingIssue, ...] = ()
    grants: tuple[SyntheticGrant, ...] = ()
    closed_transactions: frozenset[str] = frozenset()
    closed_nonces: frozenset[str] = frozenset()
    closed_reviews: frozenset[str] = frozenset()
    last_seen_ms: int = 0
    sealed: bool = False


@dataclass(frozen=True, slots=True)
class SimulationResult:
    outcome: str
    reason: str
    state: Simulation
    record: SyntheticGrant | None = None


def new_simulation(policy):
    return Simulation(_stamp(policy))


def _state(state):
    # Structural rejection is not authentication of caller-fabricated state.
    if type(state) is not Simulation or type(state.sealed) is not bool:
        raise ValueError("explicit synthetic state required")
    _stamp(state.policy)
    _number(state.revision)
    _number(state.last_seen_ms)
    for history in (state.closed_transactions, state.closed_nonces, state.closed_reviews):
        if type(history) is not frozenset or len(history) > MAX_HISTORY:
            raise ValueError("invalid history")
        for item in history:
            _hex(item)
    if (type(state.pending) is not tuple or type(state.grants) is not tuple
            or len(state.pending) + len(state.grants) > MAX_RECORDS):
        raise ValueError("invalid record containers")
    ids = []
    for pending in state.pending:
        if type(pending) is not PendingIssue or type(pending.data) is not PolicyRequestData:
            raise ValueError("invalid pending record")
        _hex(pending.tx_id)
        _number(pending.base_revision)
        _number(pending.uses)
        if not 1 <= pending.uses <= MAX_USES or pending.base_revision > state.revision:
            raise ValueError("invalid pending bounds")
        source = json.loads(pending.source_json)
        request_digest(source)
        if pending.source_json != _json(source):
            raise ValueError("noncanonical source")
        ids.append(pending.tx_id)
    for grant in state.grants:
        if type(grant) is not SyntheticGrant:
            raise ValueError("invalid synthetic grant")
        _hex(grant.grant_id)
        _stamp(grant.policy)
        _number(grant.remaining)
        _number(grant.expires_at_ms)
        candidate, source = json.loads(grant.request_json), json.loads(grant.source_json)
        request_digest(candidate)
        request_digest(source)
        if (not 1 <= grant.remaining <= MAX_USES or grant.policy != state.policy
                or grant.request_json != _json(candidate) or grant.source_json != _json(source)
                or grant.expires_at_ms != candidate["created_at_ms"] + candidate["ttl_seconds"] * 1000
                or candidate["mode"] == "once" and grant.remaining != 1):
            raise ValueError("invalid grant bounds")
        ids.append(grant.grant_id)
    if (len(ids) != len(set(ids)) or not set(ids) <= state.closed_transactions
            or state.sealed and (state.pending or state.grants)):
        raise ValueError("inconsistent model state")


def _data(data, source, uses, now_ms):
    if type(data) is not PolicyRequestData:
        raise ValueError("bridge specification data required")
    request_digest(source)
    _hex(data.review_id)
    _hex(data.source_digest)
    _hex(data.request_digest)
    _number(data.confirmed_at_ms)
    _number(data.expires_at_ms)
    _number(uses)
    if data.selection_revision is not None:
        _number(data.selection_revision)
        if data.selection_revision == 0:
            raise ValueError("invalid selection revision")
    if type(data.request_json) is not str:
        raise ValueError("canonical candidate required")
    candidate = json.loads(data.request_json)
    digest = request_digest(candidate)
    expected = json.loads(_json(source))
    if candidate["mode"] == "once":
        expected.update(mode="once", ttl_seconds=min(source["ttl_seconds"], 60))
    elif source["mode"] != "reusable" or data.selection_revision is None:
        raise ValueError("no repeat selection data")
    expiry = candidate["created_at_ms"] + candidate["ttl_seconds"] * 1000
    if (data.request_json != _json(candidate) or candidate != expected
            or data.source_digest != request_digest(source) or data.request_digest != digest
            or data.expires_at_ms != expiry
            or not candidate["created_at_ms"] <= data.confirmed_at_ms <= now_ms < expiry
            or not 1 <= uses <= MAX_USES or candidate["mode"] == "once" and uses != 1):
        raise ValueError("candidate, scope, count or original expiry mismatch")
    return candidate


def transition(state, event, context, *, now_ms):
    """One SERIAL synthetic transition; never returns execution permission.

    Context is exactly {request: current full SOURCE, policy: PolicyStamp,
    unlocked: bool}. Consume additionally carries the full selected candidate.
    Policy stamp stays fixed; a separate authorization revision advances ONCE
    per commit/consume/revoke/interrupt. Prepare reserves review+nonce+tx but
    installs nothing. Commit uses compare-and-swap on the captured revision.
    Every consume uses a fresh tx_id, debits before yielding inert receipt data,
    and never refunds on interruption. No automatic repeat-to-once fallback.

    Bad events/data deny terminally. Policy mismatch, session lock, bad context
    or time rollback clear ALL records. Other denials clear the addressed record.
    Malformed state/time raises ValueError and yields no positive result.
    """
    _state(state)
    now_ms = _number(now_ms)
    state = replace(state, last_seen_ms=max(state.last_seen_ms, now_ms))

    def deny(reason, *, all_records=False, preserve_target=False):
        tx = event.get("tx_id") if type(event) is dict else None
        target = event.get("grant_id", tx) if type(event) is dict else None
        # Early failures (policy/session/schema) must also burn recognizable
        # input IDs, or restoring context could resurrect a rejected prepare.
        histories = {}
        candidates = [("closed_transactions", tx)]
        if type(event) is dict and event.get("action") == "prepare":
            source, data = event.get("source"), event.get("data")
            candidates.extend([
                ("closed_nonces", source.get("nonce") if type(source) is dict else None),
                ("closed_reviews", data.review_id if type(data) is PolicyRequestData else None)])
        saturated = False
        for name, value in candidates:
            try:
                _hex(value)
            except ValueError:
                continue
            history = getattr(state, name)
            if value not in history and len(history) >= MAX_HISTORY:
                saturated = True
            else:
                histories[name] = history | {value}
        next_state = replace(state,
            pending=() if all_records or saturated else tuple(p for p in state.pending if p.tx_id != tx and p.tx_id != target),
            grants=() if all_records or saturated else state.grants if preserve_target else tuple(g for g in state.grants if g.grant_id != target),
            sealed=state.sealed or saturated, **histories)
        return SimulationResult("DENY", reason, next_state)

    def advance(**changes):
        if state.revision == _MAX:
            return SimulationResult("DENY", "revision_exhausted",
                                    replace(state, pending=(), grants=(), sealed=True))
        return replace(state, revision=state.revision + 1, **changes)

    if state.sealed:
        return deny("sealed", all_records=True)
    if now_ms < state.last_seen_ms:
        return deny("clock_regressed", all_records=True)
    try:
        if (type(context) is not dict or context.keys() != {"request", "policy", "unlocked"}
                or type(context["unlocked"]) is not bool):
            raise ValueError("invalid context")
        request_digest(context["request"])
        _stamp(context["policy"])
    except (ValueError, TypeError):
        return deny("invalid_context", all_records=True)
    if not context["unlocked"]:
        return deny("session_locked", all_records=True)
    if context["policy"] != state.policy or _request_stamp(context["request"]) != state.policy:
        return deny("protected_policy_changed", all_records=True)
    schemas = {
        "prepare": {"action", "tx_id", "data", "source", "uses"},
        "commit": {"action", "tx_id"},
        "consume": {"action", "tx_id", "grant_id", "request"},
        "revoke_one": {"action", "tx_id", "grant_id"},
        "revoke_all": {"action", "tx_id"},
        "interrupt": {"action", "tx_id"},
    }
    if (type(event) is not dict or type(event.get("action")) is not str
            or event["action"] not in schemas or event.keys() != schemas[event["action"]]):
        return deny("invalid_event", all_records=True)
    action, tx_id = event["action"], event["tx_id"]
    try:
        _hex(tx_id)
        if "grant_id" in event:
            _hex(event["grant_id"])
    except ValueError:
        return deny("invalid_identifier", all_records=True)
    pending = next((p for p in state.pending if p.tx_id == tx_id), None)
    if action == "interrupt":
        if tx_id not in state.closed_transactions:
            return deny("unknown_transaction")
        # Without a dispatch->grant map, unknown-outcome dispatch interruption
        # conservatively invalidates every record. A prepared issue is narrower.
        changed = advance(pending=tuple(p for p in state.pending if p.tx_id != tx_id),
                          grants=() if pending is None else state.grants)
        if type(changed) is SimulationResult:
            return changed
        return SimulationResult("DENY", "interrupted_no_retry", changed)
    if action == "commit":
        if pending is None:
            return deny("closed_or_unknown_transaction")
        try:
            _data(pending.data, json.loads(pending.source_json), pending.uses, now_ms)
        except (ValueError, TypeError):
            return deny("invalid_or_expired_pending")
        if pending.source_json != _json(context["request"]):
            return deny("source_or_binding_changed")
        if pending.base_revision != state.revision:
            return deny("authorization_revision_changed")
        record = SyntheticGrant(tx_id, pending.source_json, pending.data.request_json,
                                state.policy, pending.data.expires_at_ms, pending.uses)
        changed = advance(pending=tuple(p for p in state.pending if p.tx_id != tx_id),
                          grants=state.grants + (record,))
        if type(changed) is SimulationResult:
            return changed
        return SimulationResult("SYNTHETIC_ISSUED_ONLY", "atomic_record_install", changed, record)
    # Reserve every fresh operation before validating its untrusted contents.
    if tx_id in state.closed_transactions:
        return deny("duplicate_transaction")
    if len(state.closed_transactions) >= MAX_HISTORY:
        return SimulationResult("DENY", "history_full",
                                replace(state, pending=(), grants=(), sealed=True))
    state = replace(state, closed_transactions=state.closed_transactions | {tx_id})
    if action == "prepare":
        data, source = event["data"], event["source"]
        nonce = source.get("nonce") if type(source) is dict else None
        review = data.review_id if type(data) is PolicyRequestData else None
        try:
            _hex(nonce)
            _hex(review)
        except ValueError:
            return deny("invalid_source_or_review")
        if nonce in state.closed_nonces or review in state.closed_reviews:
            return deny("closed_source_or_review")
        if max(len(state.closed_nonces), len(state.closed_reviews)) >= MAX_HISTORY:
            return SimulationResult("DENY", "history_full",
                                    replace(state, pending=(), grants=(), sealed=True))
        state = replace(state, closed_nonces=state.closed_nonces | {nonce},
                        closed_reviews=state.closed_reviews | {review})
        try:
            _data(data, source, event["uses"], now_ms)
        except (ValueError, TypeError):
            return deny("invalid_or_expired_candidate")
        if _json(source) != _json(context["request"]):
            return deny("source_or_binding_changed")
        if len(state.pending) + len(state.grants) >= MAX_RECORDS:
            return deny("record_capacity")
        record = PendingIssue(tx_id, data, _json(source), event["uses"], state.revision)
        return SimulationResult("SYNTHETIC_PREPARED_ONLY", "no_grant_installed",
                                replace(state, pending=state.pending + (record,)))
    if action == "revoke_all":
        owner = context["request"]["binding"]["owner_sid"]
        changed = advance(
            pending=tuple(p for p in state.pending if json.loads(p.source_json)["binding"]["owner_sid"] != owner),
            grants=tuple(g for g in state.grants if json.loads(g.source_json)["binding"]["owner_sid"] != owner))
        if type(changed) is SimulationResult:
            return changed
        return SimulationResult("SYNTHETIC_REVOKED_ONLY", "owner_records_removed", changed)
    grant = next((g for g in state.grants if g.grant_id == event["grant_id"]), None)
    if grant is None:
        return deny("unknown_or_closed_grant")
    if action == "revoke_one":
        if json.loads(grant.source_json)["binding"]["owner_sid"] != context["request"]["binding"]["owner_sid"]:
            return deny("wrong_revoking_owner", preserve_target=True)
        changed = advance(grants=tuple(g for g in state.grants if g != grant))
        if type(changed) is SimulationResult:
            return changed
        return SimulationResult("SYNTHETIC_REVOKED_ONLY", "one_record_removed", changed)
    try:
        request_digest(event["request"])
    except (ValueError, TypeError):
        return deny("invalid_execution_snapshot")
    if grant.source_json != _json(context["request"]) or grant.request_json != _json(event["request"]):
        return deny("exact_scope_or_binding_changed")
    candidate = json.loads(grant.request_json)
    if not candidate["created_at_ms"] <= now_ms < grant.expires_at_ms:
        return deny("expired_or_invalid_time")
    records = tuple(g for g in state.grants if g != grant)
    if grant.remaining > 1:
        records += (replace(grant, remaining=grant.remaining - 1),)
    changed = advance(grants=records)
    if type(changed) is SimulationResult:
        return changed
    return SimulationResult("SYNTHETIC_CONSUMED_ONLY", "debited_no_dispatch", changed)
