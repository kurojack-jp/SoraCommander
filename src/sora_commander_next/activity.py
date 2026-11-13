"""Bounded, data-only current-process activity. No execution or persistence."""

from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re
from threading import RLock
from time import monotonic

SCOPE = "CURRENT_EXECUTION_PROCESS"
STATES = frozenset({
    "PENDING", "REVIEW", "RUNNING", "COMPLETED", "FAILED", "ERROR", "DENY",
    "DENIED", "UNKNOWN", "OUTCOME_UNKNOWN", "BUSY", "CANCELLING", "CANCELLED",
    "TIMEOUT", "TIMED_OUT", "OUTPUT_LIMIT", "OUTPUT_INCOMPLETE", "SENT",
    "CREATED_LOCAL_DEV", "REPLACED_LOCAL_DEV", "UNCHANGED_LOCAL_DEV",
    "DENY_DUPLICATE_UNKNOWN", "DENY_DUPLICATE_EXPIRED", "DENY_DUPLICATE_CONFLICT",
    "DENY_REPLAY_GUARD_FULL", "DENY_UNPROTECTED_WORKER",
})
ACTIVE = frozenset({"PENDING", "REVIEW", "RUNNING", "CANCELLING"})
KINDS = {"request_id": 32, "pending_id": 32, "session_id": 48}
OPERATIONS = {"command", "send_input", "text_edit", "text_create", "unknown"}
SOURCES = {"job", "review", "input", "edit", "retired", "activity_only"}
ACTIONS = {"OBSERVE_EXISTING", "MANUAL_RECONCILIATION", "REPORT_AND_WAIT",
           "CHOOSE_LABELLED_ID", "CORRECT_READ_ARGUMENTS"}
ROUTES = {"request_status": "request_id", "review_status": "pending_id",
          "edit_review_status": "pending_id", "list_sessions": None,
          "recovery_overview": None, None: None}


class InvalidMetadata(ValueError):
    pass


def valid_id(value, size=None):
    return (type(value) is str and len(value) in ((size,) if size else (32, 48))
            and re.fullmatch(r"[0-9a-f]+", value) is not None)


def arguments(values):
    if type(values) is not dict or set(values) - {"mode", "id", "limit"}:
        raise ValueError("invalid arguments")
    mode, identity, limit = values.get("mode", "recent"), values.get("id"), values.get("limit", 10)
    if (type(mode) is not str or mode not in {"recent", "lookup"}
            or type(limit) is not int or not 1 <= limit <= 20
            or (mode == "recent" and identity is not None)
            or (mode == "lookup" and (not valid_id(identity) or limit != 10))):
        raise ValueError("invalid arguments")
    return mode, identity, limit


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("utf-8"))


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Record:
    operation_type: str = "command"
    request_id: str | None = None
    pending_id: str | None = None
    session_id: str | None = None
    state: str | None = None
    review_state: str | None = None
    job_state: str | None = None
    state_source: str = "activity_only"
    created_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    time_basis: str = "PRODUCER_EVENT"
    next_tool: str | None = None
    next_args: dict | None = None
    required_action: str = "REPORT_AND_WAIT"
    auto_retry: bool = False

    def data(self):
        value = asdict(self)
        if value["next_args"] is None:
            value["next_args"] = {}
        validate_record(value)
        return value


RECORD_KEYS = frozenset(Record.__dataclass_fields__)
REPLY_KEYS = frozenset({"status", "mode", "scope", "query_id", "id_kind", "records",
    "count", "truncated", "error", "next_tool", "next_args", "required_action", "auto_retry"})


def _route(value):
    tool, args = value["next_tool"], value["next_args"]
    if tool not in ROUTES or type(args) is not dict:
        raise InvalidMetadata()
    key = ROUTES[tool]
    if set(args) != ({key} if key else set()):
        raise InvalidMetadata()
    if key and not valid_id(args[key], KINDS[key]):
        raise InvalidMetadata()
    if value["required_action"] not in ACTIONS or value["auto_retry"] is not False:
        raise InvalidMetadata()


def validate_record(value):
    if type(value) is not dict or set(value) != RECORD_KEYS:
        raise InvalidMetadata()
    if value["operation_type"] not in OPERATIONS or value["state_source"] not in SOURCES:
        raise InvalidMetadata()
    for key, size in KINDS.items():
        if value[key] is not None and not valid_id(value[key], size):
            raise InvalidMetadata()
    for key in ("state", "review_state", "job_state"):
        if value[key] is not None and (type(value[key]) is not str or value[key] not in STATES):
            raise InvalidMetadata()
    for key in ("created_at", "updated_at", "finished_at"):
        stamp = value[key]
        if stamp is not None:
            if type(stamp) is not str or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", stamp):
                raise InvalidMetadata()
            try:
                datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                raise InvalidMetadata() from None
    if value["time_basis"] != "PRODUCER_EVENT":
        raise InvalidMetadata()
    _route(value)
    key = ROUTES[value["next_tool"]]
    if key and value["next_args"][key] != value[key]:
        raise InvalidMetadata()
    if value["state_source"] == "job" and value["state"] != value["job_state"]:
        raise InvalidMetadata()
    if value["state_source"] == "retired" and (
            value["state"] != "OUTCOME_UNKNOWN" or value["job_state"] is not None):
        raise InvalidMetadata()
    if value["operation_type"] in {"send_input", "text_edit", "text_create"} and value["job_state"] is not None:
        raise InvalidMetadata()
    created, updated, finished = (value[k] for k in ("created_at", "updated_at", "finished_at"))
    if (created is None) != (updated is None) or (finished is not None and created is None):
        raise InvalidMetadata()
    if created is not None:
        convert = lambda stamp: datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if convert(created) > convert(updated) or (finished and not convert(created) <= convert(finished) <= convert(updated)):
            raise InvalidMetadata()
    if _bytes(value) > 2048:
        raise InvalidMetadata()


def reply(mode=None, identity=None, *, status="OK", kind=None, records=(),
          truncated=False, error=None, tool=None, args=None, action="REPORT_AND_WAIT"):
    return dict(status=status, mode=mode, scope=SCOPE, query_id=identity, id_kind=kind,
                records=list(records), count=len(records), truncated=truncated, error=error,
                next_tool=tool, next_args=args or {}, required_action=action, auto_retry=False)


def validate_reply(value, mode, identity):
    if (type(value) is not dict or set(value) != REPLY_KEYS or value["scope"] != SCOPE
            or value["mode"] != mode or value["query_id"] != identity
            or value["status"] not in {"OK", "UNKNOWN", "AMBIGUOUS", "ERROR", "UNAVAILABLE", "OUTCOME_UNKNOWN"}
            or value["id_kind"] not in {*KINDS, "unknown", "ambiguous", None}
            or type(value["records"]) is not list or len(value["records"]) > 20
            or type(value["count"]) is not int or value["count"] != len(value["records"])
            or type(value["truncated"]) is not bool
            or value["error"] not in {None, "InvalidArguments", "InvalidMetadata", "ActivityUnavailable",
                "ProtectedBrokerUnavailable", "ConnectionClosed", "ActivityUnsupported", "BrokerReplyUnconfirmed", "TooManyMatches"}):
        raise InvalidMetadata()
    _route(value)
    for row in value["records"]:
        validate_record(row)
    if _bytes(value) > 32768:
        raise InvalidMetadata()
    if value["status"] in {"ERROR", "UNAVAILABLE", "OUTCOME_UNKNOWN", "UNKNOWN"} and value["records"]:
        raise InvalidMetadata()
    status, rows = value["status"], value["records"]
    if status == "OK":
        if value["error"] is not None:
            raise InvalidMetadata()
        if mode == "recent":
            if (value["id_kind"] is not None or value["next_tool"] is not None
                    or value["required_action"] != "CHOOSE_LABELLED_ID"):
                raise InvalidMetadata()
        elif (mode != "lookup" or len(rows) != 1 or value["id_kind"] not in KINDS
              or rows[0][value["id_kind"]] != identity or value["truncated"]
              or sum(rows[0][k] == identity for k in KINDS) != 1
              or any(value[k] != rows[0][k] for k in ("next_tool", "next_args", "required_action"))):
            raise InvalidMetadata()
    elif status == "UNKNOWN":
        if (mode != "lookup" or value["id_kind"] != "unknown" or value["error"] is not None
                or value["truncated"] or value["next_tool"] != "recovery_overview"
                or value["required_action"] != "MANUAL_RECONCILIATION"):
            raise InvalidMetadata()
    elif status == "AMBIGUOUS":
        if (mode != "lookup" or value["id_kind"] != "ambiguous" or value["next_tool"] is not None
                or value["required_action"] != "MANUAL_RECONCILIATION"
                or any(identity not in (r[k] for k in KINDS) for r in rows)
                or (not rows and (value["error"] != "TooManyMatches" or not value["truncated"]))
                or (rows and (value["error"] is not None or value["truncated"]
                    or (len(rows) == 1 and sum(rows[0][k] == identity for k in KINDS) < 2)))):
            raise InvalidMetadata()
    else:
        allowed = {"ERROR": {"InvalidArguments", "InvalidMetadata", "ActivityUnavailable"},
                   "UNAVAILABLE": {"ProtectedBrokerUnavailable", "ConnectionClosed", "ActivityUnsupported"},
                   "OUTCOME_UNKNOWN": {"BrokerReplyUnconfirmed"}}
        if (value["id_kind"] is not None or value["truncated"] or value["next_tool"] is not None
                or value["error"] not in allowed[status]
                or value["required_action"] != ("CORRECT_READ_ARGUMENTS" if value["error"] == "InvalidArguments" else "REPORT_AND_WAIT")):
            raise InvalidMetadata()
    return value


class ActivityStore:
    """512 admission-ordered rows; logical TTL reads never mutate the store."""
    def __init__(self, *, clock=monotonic, utc=utc_now):
        self._clock, self._utc = clock, utc
        self._lock = RLock()
        self._rows = OrderedDict()
        self._invalid = False

    def invalidate(self):
        with self._lock:
            self._invalid = True

    def publish(self, *, request_id=None, pending_id=None, session_id=None,
                state, source, review_state=None, job_state=None, operation_type="command"):
        # Explicit scalar-only signature; no Command or result dictionaries.
        if request_id is not None and not valid_id(request_id, 32):
            return  # legacy internal identities are outside this API
        identity = (request_id or pending_id or session_id) if operation_type == "command" else pending_id
        if not valid_id(identity) or operation_type not in OPERATIONS - {"unknown"}:
            raise InvalidMetadata()
        key = (operation_type, identity)
        with self._lock:
            now, stamp = self._clock(), self._utc()
            self._rows = OrderedDict((k, v) for k, v in self._rows.items() if now - v[0] < 3600)
            prior = self._rows.get(key)
            old = prior[1] if prior else Record().data()
            if old["job_state"] is not None and source != "job":
                state, source, job_state = old["job_state"], "job", old["job_state"]
            value = Record(operation_type=operation_type, request_id=request_id or old["request_id"],
                pending_id=pending_id or old["pending_id"], session_id=session_id or old["session_id"],
                state=state, review_state=review_state or old["review_state"],
                job_state=job_state or old["job_state"], state_source=source,
                created_at=old["created_at"] or stamp, updated_at=stamp,
                finished_at=(old["finished_at"] or stamp) if state not in ACTIVE else None).data()
            if prior and any(old[k] and value[k] != old[k] for k in KINDS):
                raise InvalidMetadata()
            if prior and all(value[k] == old[k] for k in (
                    *KINDS, "state", "review_state", "job_state", "state_source")):
                return
            self._rows[key] = (prior[0] if prior else now, value)
            while len(self._rows) > 512:
                self._rows.popitem(last=False)

    def snapshot(self):
        with self._lock:
            if self._invalid:
                raise InvalidMetadata()
            now = self._clock()
            # Copy nested route dictionaries too; callers cannot alter retention.
            return [dict(v, next_args=dict(v["next_args"])) for admitted, v in reversed(self._rows.values())
                    if now - admitted < 3600]


def publish(store, **scalars):
    """Observation failure is independent of every execution/replay decision."""
    if store is None:
        return
    try:
        store.publish(**scalars)
    except Exception:
        try:
            store.invalidate()
        except Exception:
            pass


def _routed(row, *, edit_registered=False):
    value = dict(row, next_args={})
    tool, key, action = None, None, "REPORT_AND_WAIT"
    source, state = value["state_source"], value["state"]
    if source == "retired":
        if value["request_id"]:
            tool, key = "request_status", "request_id"
        action = "MANUAL_RECONCILIATION"
    elif (source == "activity_only" and state == "OUTCOME_UNKNOWN"
          and value["operation_type"] in {"text_edit", "text_create"}):
        action = "MANUAL_RECONCILIATION"
    elif source == "job":
        tool, key = (("request_status", "request_id") if value["request_id"]
                     else ("list_sessions", None))
        action = "OBSERVE_EXISTING"
    elif source in {"input", "edit"} and state == "TIMED_OUT":
        pass
    elif source in {"input", "edit"}:
        if source == "input" or edit_registered:
            tool, key = ("review_status" if source == "input" else "edit_review_status"), "pending_id"
            action = "MANUAL_RECONCILIATION" if state in {"UNKNOWN", "OUTCOME_UNKNOWN"} else "OBSERVE_EXISTING"
    elif source == "review" and state in ACTIVE and value["pending_id"]:
        tool, key, action = "review_status", "pending_id", "OBSERVE_EXISTING"
    value.update(next_tool=tool, next_args={key: value[key]} if key else {}, required_action=action)
    validate_record(value)
    return value


def _merge(base, source):
    value = dict(base)
    for key in KINDS:
        if base[key] is not None and source[key] is not None and base[key] != source[key]:
            raise InvalidMetadata()
        value[key] = source[key] or base[key]
    value.update(state=source["state"], state_source=source["state_source"],
                 job_state=source["job_state"], review_state=source["review_state"] or base["review_state"])
    return value


def _related(left, right):
    return (left["operation_type"] == right["operation_type"]
            and any(left[k] is not None and left[k] == right[k] for k in KINDS)
            and all(left[k] is None or right[k] is None or left[k] == right[k] for k in KINDS))


def observe(host, values):
    try:
        mode, identity, limit = arguments(values)
    except (ValueError, TypeError):
        return reply(status="ERROR", error="InvalidArguments", action="CORRECT_READ_ARGUMENTS")
    try:
        cached = host._activity_store.snapshot()
        identities = {identity} if mode == "lookup" else {
            row[k] for row in cached for k in KINDS if row[k] is not None}
        # These locks are acquired independently; no nested source locks.
        approvals = host.approval.activity_snapshot()
        reviews = (() if host._owner_review_dispatcher is None
                   else host._owner_review_dispatcher.activity_snapshot())
        correlation = {}
        for rid, pid, active in approvals:
            if not valid_id(rid, 32):
                continue
            correlation[pid] = (rid, active)
            if mode == "lookup" and identity in (rid, pid):
                identities.update((rid, pid))
        for row in cached:
            if identity is not None and identity in (row[k] for k in KINDS):
                identities.update(row[k] for k in KINDS if row[k] is not None)
        review_rows = []
        for pid, state, sid in reviews:
            rid = correlation.get(pid, (None, False))[0]
            if pid in identities or rid in identities or sid in identities:
                if rid:
                    identities.add(rid)
                review_rows.append(Record(request_id=rid, pending_id=pid, session_id=sid,
                    state=state, review_state=state, state_source="review").data())
        for pid, (rid, active) in correlation.items():
            if (pid in identities or rid in identities) and all(r["pending_id"] != pid for r in review_rows):
                review_rows.append(Record(request_id=rid, pending_id=pid,
                    state="PENDING" if active else "OUTCOME_UNKNOWN",
                    review_state="PENDING" if active else None, state_source="review").data())
        commands = []
        for manager in (host.jobs, host._dev_interactive_jobs):
            if manager is not None:
                commands.extend(manager.activity_snapshot(identities))
        pilot = getattr(host, "_edit_review_pilot", None)
        if pilot is not None:
            commands.extend(pilot.activity_snapshot(identities))
        for review in review_rows:
            matches = [i for i, row in enumerate(commands) if _related(review, row)]
            if len(matches) == 1:
                i = matches[0]
                merged = _merge(review, commands[i])
                merged["review_state"] = review["review_state"]
                commands[i] = merged
            elif len(matches) > 1:
                # Preserve each operation; never select the first manager.
                for i in matches:
                    commands[i] = _merge(review, commands[i])
            else:
                commands.append(review)
        rows = []
        consumed = set()
        for old in cached:
            matches = [i for i, row in enumerate(commands) if _related(old, row)]
            if matches:
                for i in matches:
                    if i not in consumed:
                        rows.append(_merge(old, commands[i]))
                        consumed.add(i)
            else:
                state = (old["state"] if old["state_source"] == "activity_only"
                         and old["state"] not in ACTIVE else "OUTCOME_UNKNOWN")
                value = dict(old, state=state, job_state=None, state_source="activity_only")
                rows.append(value)
        if mode == "lookup":
            rows.extend(row for i, row in enumerate(commands) if i not in consumed)
            rows = [row for row in rows if identity in (row[k] for k in KINDS)]
        rows = [_routed(row, edit_registered=pilot is not None) for row in rows]
        if mode == "recent":
            result = reply(mode, records=rows[:limit], truncated=len(rows) > limit,
                           action="CHOOSE_LABELLED_ID")
        elif not rows:
            result = reply(mode, identity, status="UNKNOWN", kind="unknown",
                           tool="recovery_overview", action="MANUAL_RECONCILIATION")
        else:
            kinds = {k for row in rows for k in KINDS if row[k] == identity}
            ambiguous = len(rows) > 1 or len(kinds) != 1
            if len(rows) > 20:
                result = reply(mode, identity, status="AMBIGUOUS", kind="ambiguous", truncated=True,
                               error="TooManyMatches", action="MANUAL_RECONCILIATION")
            elif ambiguous:
                result = reply(mode, identity, status="AMBIGUOUS", kind="ambiguous", records=rows,
                               action="MANUAL_RECONCILIATION")
            else:
                row = rows[0]
                result = reply(mode, identity, kind=next(iter(kinds)), records=rows,
                    tool=row["next_tool"], args=row["next_args"], action=row["required_action"])
        return validate_reply(result, mode, identity)
    except (InvalidMetadata, ValueError, TypeError, KeyError):
        return reply(mode, identity, status="ERROR", error="InvalidMetadata")
    except Exception:
        return reply(mode, identity, status="ERROR", error="ActivityUnavailable")
