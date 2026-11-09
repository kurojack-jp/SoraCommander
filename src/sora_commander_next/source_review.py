"""DEV_ONLY general-source Owner review workflow; never a product authority.

The production factory cannot construct this workflow. An injected store must
implement atomic identity+content CAS (including ancestor identity), call the
last Owner check while holding its exclusion, and never retry a mutation.
The disk reader intentionally refuses patch commits: Python os.replace is not
such a CAS. DevelopmentExclusiveCreateStore supports trusted-tree create only.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import difflib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
from secrets import token_hex
import stat
from threading import Lock, RLock
from time import monotonic

from .broker_host import encode_frame, decode_frame, validate_source_arguments
from .file_edits import _SOURCE_EXTENSIONS, literal_occurs_once
from .offline_source_patch_candidate import inspect_patch_bytes

MAX_SOURCE = 16384
_MANAGED = {"node_modules", "build", "dist", "target", "__pycache__", "bin", "obj"}


class SourceConflict(ValueError):
    """Store attests that it performed NO mutation; no retry is authorized."""


def _literal(value):
    if type(value) is not str or not value or len(value) > 2048:
        raise ValueError("literal local source path required")
    path = PureWindowsPath(value)
    if (re.fullmatch("[A-Z]:", path.drive) is None or not path.is_absolute()
            or str(path) != value or not path.parts[1:]):
        raise ValueError("local canonical drive path required")
    for part in path.parts[1:]:
        if (part in (".", "..") or part.startswith(".") or part.endswith((" ", "."))
                or part.casefold() in _MANAGED or "~" in part
                or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in part)
                or re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", part)):
            raise ValueError("private, generated or ambiguous source path")
    return path


class SourcePolicy:
    """Locally constructed exact root list; never comes from a tool request."""
    def __init__(self, roots=()):
        if type(roots) is not tuple or len(roots) > 8 or len(set(roots)) != len(roots):
            raise ValueError("bounded explicit source root tuple required")
        self._roots = tuple(str(_literal(root)) for root in roots)

    def target(self, path):
        candidate = _literal(path)
        if (candidate.suffix.casefold() not in _SOURCE_EXTENSIONS
                or not any(path.startswith(root + "\\") for root in self._roots)):
            raise ValueError("source outside exact local allow roots")
        return path


@dataclass(frozen=True)
class SourceSnapshot:
    data: bytes | None
    identity: tuple | None
    parents: tuple

    def __post_init__(self):
        if (self.data is not None and (type(self.data) is not bytes or len(self.data) > MAX_SOURCE)):
            raise ValueError("bounded source bytes required")
        if self.data is not None:
            self.data.decode("utf-8")
        if (self.data is None) != (self.identity is None):
            raise ValueError("absent identity mismatch")
        if self.identity is not None and (type(self.identity) is not tuple
                or len(self.identity) != 5 or any(type(n) is not int or n < 0 for n in self.identity)
                or self.identity[1] == 0 or self.identity[2] != len(self.data)):
            raise ValueError("strong file identity/version required")
        if (type(self.parents) is not tuple or not self.parents
                or any(type(p) is not tuple or len(p) != 3 or type(p[0]) is not str
                       or any(type(n) is not int or n < 0 for n in p[1:]) or p[2] == 0
                       for p in self.parents)):
            raise ValueError("exact ancestor identities required")


@dataclass(frozen=True, slots=True)
class SourceWriteProof:
    """DEV diagnostic proof bound to the complete one-shot review, not consent.

    Ordinary forgeable Python data. Never accepted by a writer as authority.
    Kept inside the terminal workflow record; no deserialize/rearm interface.
    """
    review_bytes: bytes
    before: SourceSnapshot
    after: SourceSnapshot
    completed_tick: int
    authority: str = field(default="NONE_DEV_DIAGNOSTIC", init=False)

    def __post_init__(self):
        frame = decode_frame(self.review_bytes)
        if (type(self.before) is not SourceSnapshot or type(self.after) is not SourceSnapshot
                or type(self.completed_tick) is not int
                or not frame["issued_tick"] <= self.completed_tick < frame["expires_tick"]
                or re.fullmatch("[0-9a-f]{64}", frame["nonce"]) is None
                or self.after.data is None or self.after.identity is None
                or self.before.parents != self.after.parents
                or self.after.data != frame["after_utf8"].encode("utf-8")
                or sha256(self.after.data).hexdigest() != frame["after_sha256"]
                or frame["operation"] != ("create" if self.before.data is None else "patch")
                or frame["identity"] != (None if self.before.identity is None else list(self.before.identity))
                or frame["parents"] != [list(p) for p in self.before.parents]
                or frame["before_utf8"].encode("utf-8") != (self.before.data or b"")
                or frame["before_sha256"] != (None if self.before.data is None
                    else sha256(self.before.data).hexdigest())):
            raise ValueError("write proof does not match complete reviewed proposal")


def _chain(path):
    records = []
    for parent in (path, *path.parents):
        info = parent.lstat()
        # Windows volume roots normally carry HIDDEN|SYSTEM (e.g. D:\\).
        # HIDDEN is a source-directory restriction below that root. Reparse
        # and missing identity remain forbidden at EVERY ancestor.
        denied_attributes = 0x400 if parent.parent == parent else 0x402
        if (not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & denied_attributes
                or info.st_ino == 0):
            raise SourceConflict("unverifiable source ancestor")
        records.append((str(parent), info.st_dev, info.st_ino))
    return tuple(records)


def _version(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class ReadOnlySourceStore:
    """Real bounded byte/identity inspection. No assumed race-safe patch API."""
    def __init__(self, policy):
        self.policy = policy
        self._roots = tuple(_chain(Path(root)) for root in policy._roots)

    def snapshot(self, path):
        p = Path(self.policy.target(path))
        if tuple(_chain(Path(root)) for root in self.policy._roots) != self._roots:
            raise SourceConflict("configured source root changed")
        parents = _chain(p.parent)
        try:
            first = p.lstat()
        except FileNotFoundError:
            if _chain(p.parent) != parents:
                raise SourceConflict("source parent changed")
            return SourceSnapshot(None, None, parents)
        if (not stat.S_ISREG(first.st_mode) or first.st_nlink != 1
                or first.st_size > MAX_SOURCE or getattr(first, "st_file_attributes", 0) & 0x402):
            raise SourceConflict("ordinary single-link visible UTF-8 source required")
        with p.open("rb") as source:
            opened = os.fstat(source.fileno())
            data = source.read(MAX_SOURCE + 1)
            after = os.fstat(source.fileno())
        if (_version(opened) != _version(after) or _version(first) != _version(p.lstat())
                or _version(opened)[:4] != _version(first)[:4]
                or opened.st_nlink != 1 or after.st_nlink != 1
                or parents != _chain(p.parent)):
            raise SourceConflict("source changed during snapshot")
        return SourceSnapshot(data, _version(after), parents)

    def commit(self, path, before, data, last_check):
        raise SourceConflict("STRONG_SOURCE_CAS_NOT_ACCEPTED")


class DevelopmentExclusiveCreateStore(ReadOnlySourceStore):
    """DEV trusted stable directory tree ONLY, not hostile rename protection.

    Exclusive create never replaces a target. There is deliberately no existing
    source patch fallback to cooperative os.replace. Native ancestor handles,
    protected scope/ACL enforcement and Windows acceptance remain blockers.
    """
    def commit(self, path, before, data, last_check):
        if (type(before) is not SourceSnapshot or type(data) is not bytes
                or len(data) > MAX_SOURCE or b"\0" in data or not callable(last_check)):
            raise SourceConflict("invalid bounded source create input")
        data.decode("utf-8")
        if before.data is not None:
            raise SourceConflict("STRONG_SOURCE_CAS_NOT_ACCEPTED")
        if self.snapshot(path) != before:
            raise SourceConflict("source appeared or parents changed")
        last_check()
        try:
            out = Path(path).open("xb")
        except FileExistsError:
            raise SourceConflict("exclusive create lost race") from None
        # Once the exclusive open succeeds, EVERY failure is outcome-unknown.
        # Retain any partial file. Never unlink, rollback or repeat creation.
        try:
            with out:
                identity = os.fstat(out.fileno())
                if (not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1
                        or _chain(Path(path).parent) != before.parents):
                    raise OSError("created target identity unconfirmed")
                # Exclusive open can block or allow a reentrant cancellation.
                # Recheck live consent before writing; original absence cannot
                # be compared after our own successful filename reservation.
                last_check(check_snapshot=False)
                if out.write(data) != len(data):
                    raise OSError("incomplete source create")
                out.flush()
                os.fsync(out.fileno())
                last_check(check_snapshot=False)
            result = self.snapshot(path)
            if (result.data != data or result.identity[:2] != (identity.st_dev, identity.st_ino)
                    or result.parents != before.parents):
                raise OSError("created source proof unconfirmed")
            return result
        except SourceConflict as exc:
            # snapshot/ancestor checks can raise the pre-write conflict type.
            # After exclusive open that must NEVER mean zero bytes were written.
            raise OSError("created source outcome unconfirmed") from exc


class LocalSourceOwner:
    """Explicit local DEV UI adapter; construction invokes real Owner checks.

    Not constructed by Host, product root, imports or tests in this task.
    Reuses the existing complete-review GUI and WTS session-event latch.
    """
    def __init__(self):
        from .windows_owner import OwnerSessionGuard
        self._guard = OwnerSessionGuard()

    def verify(self):
        return self._guard.verify()

    def binding(self):
        self.verify()
        identity = self._guard._identity
        return (identity.sid, identity.logon_luid, identity.session_id)

    def review(self, raw, last_check):
        from .file_edits import _show_edit_review
        from .windows_session_events import OwnerSessionEventLatch
        frame = decode_frame(raw)
        latch = OwnerSessionEventLatch(self._guard._identity.session_id).start()
        try:
            last_check()
            latch.verify()
            selected = _show_edit_review(
                {"path": frame["path"], "diff": frame["diff"]},
                complete_review=json.dumps(frame, ensure_ascii=True, indent=2),
                deadline=frame["expires_tick"] / 1000)
            latch.verify()
            last_check()
            return selected is True
        finally:
            latch.close()


class SourceReviewWorkflow:
    """One Broker-owned active GUI, 64 retained one-shot request records.

    No remotely callable approve/commit. Only the Broker scheduler calls
    review_local. Request IDs observe/cancel, never approve. State is volatile;
    this DEV workflow is not durable consent across Broker restart.
    """
    def __init__(self, *, policy, store, owner, clock=monotonic):
        if type(policy) is not SourcePolicy:
            raise TypeError("local source policy required")
        self._policy, self._store, self._owner, self._clock = policy, store, owner, clock
        self._lock, self._slot = RLock(), Lock()
        self._records = {}
        self._halted = False
        self._last_tick = -1

    def _tick(self):
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError("invalid monotonic clock")
        tick = int(now * 1000)
        if tick < self._last_tick:
            self._halted = True
            raise ValueError("clock rollback")
        self._last_tick = tick
        return tick

    def _binding(self):
        if self._owner.verify() is not True:
            raise PermissionError("Owner unavailable")
        binding = self._owner.binding()
        if (type(binding) is not tuple or len(binding) != 3
                or type(binding[0]) is not str or re.fullmatch(r"S-1-(?:[0-9]+-)+[0-9]+", binding[0]) is None
                or type(binding[1]) is not tuple or len(binding[1]) != 2
                or any(type(n) is not int or not 0 <= n <= 0xffffffff for n in binding[1])
                or type(binding[2]) is not int or not 1 <= binding[2] <= 0xffffffff):
            raise PermissionError("exact SID/LUID/WTS required")
        return binding

    def submit(self, action, arguments):
        validate_source_arguments(action, arguments)
        # Detach incoming mutable dictionaries before any Owner callback.
        arguments = decode_frame(encode_frame(arguments))
        fingerprint = sha256(encode_frame({"action": action, "arguments": arguments})).hexdigest()
        rid = arguments["request_id"]
        with self._lock:
            if rid in self._records:
                record = self._records[rid]
                if record["fingerprint"] != fingerprint:
                    return {"status": "DENIED", "error": "RequestConflict"}
                return self.status(rid)
            if self._halted or len(self._records) >= 64:
                return {"status": "DENIED", "error": "ReviewAdmissionStoppedOrFull"}
            path = self._policy.target(arguments["path"])
            before = self._store.snapshot(path)
            if type(before) is not SourceSnapshot:
                raise ValueError("exact source snapshot required")
            # Revalidate and detach even an injected provider's frozen object.
            before = SourceSnapshot(before.data, before.identity, before.parents)
            if action == "request_source_create":
                if before.data is not None:
                    raise SourceConflict("source already exists")
                content = arguments["content"]
            elif action == "request_source_patch":
                if (before.data is None or sha256(before.data).hexdigest() != arguments["expected_sha256"]):
                    raise SourceConflict("stale source digest")
                old = before.data.decode("utf-8")
                if not literal_occurs_once(old, arguments["old_text"]):
                    raise SourceConflict("ambiguous literal source anchor")
                content = old.replace(arguments["old_text"], arguments["new_text"], 1)
            else:
                raise ValueError("source submission action required")
            data = content.encode("utf-8")
            if (len(data) > MAX_SOURCE or "\0" in content
                    or sha256(data).hexdigest() != arguments["expected_new_sha256"]):
                raise SourceConflict("proposed source digest/size mismatch")
            # DEV_ONLY: bind the pure before/after digest check to the actual
            # Owner-reviewed patch proposal. This is never write authority.
            if action == "request_source_patch":
                candidate = inspect_patch_bytes(before.data, data, arguments["expected_sha256"])
                if (not candidate.changed
                        or candidate.authority != "NONE_PREVIEW_ONLY"
                        or candidate.new_sha256 != arguments["expected_new_sha256"]
                        or candidate.old_sha256 != arguments["expected_sha256"]):
                    raise SourceConflict("data-only patch evidence mismatch")
            owner = self._binding()
            tick = self._tick()
            previous = "" if before.data is None else before.data.decode("utf-8")
            diff = "".join(difflib.unified_diff(previous.splitlines(keepends=True),
                content.splitlines(keepends=True), fromfile="absent" if before.data is None else "current",
                tofile="proposed"))
            frame = encode_frame({"v": 1, "request_id": rid, "path": path,
                "operation": "create" if before.data is None else "patch",
                "before_sha256": None if before.data is None else sha256(before.data).hexdigest(),
                "after_sha256": sha256(data).hexdigest(), "before_utf8": previous,
                "after_utf8": content, "diff": diff, "identity": before.identity,
                "parents": before.parents, "owner_sid": owner[0], "logon_luid": owner[1],
                "wts_session": owner[2], "nonce": token_hex(32),
                "issued_tick": tick, "expires_tick": tick + 45000})
            self._records[rid] = {"fingerprint": fingerprint, "raw": frame, "before": before,
                "data": data, "owner": owner, "state": "REVIEW", "result": None}
            return self.status(rid)

    def status(self, request_id):
        with self._lock:
            self._binding()  # Pending IDs never authorize disclosure.
            record = self._records.get(request_id)
            if record is None:
                return {"status": "UNKNOWN", "request_id": request_id}
            if record["result"] is not None:
                return decode_frame(encode_frame(record["result"]))
            return {"status": record["state"], "request_id": request_id,
                    "proposal_sha256": sha256(record["raw"]).hexdigest()}

    def cancel(self, request_id):
        with self._lock:
            self._binding()
            record = self._records.get(request_id)
            if record is not None and record["state"] in {"REVIEW", "REVIEWING"}:
                record["state"] = "CANCELLED"
            return self.status(request_id)

    def review_local(self, request_id):
        if not self._slot.acquire(blocking=False):
            return {"status": "BUSY"}
        attempted = False
        commit_returned = False
        record = None
        try:
            with self._lock:
                record = self._records.get(request_id)
                if record is None or record["state"] != "REVIEW":
                    return self.status(request_id)
                record["state"] = "REVIEWING"  # Consume GUI opportunity before callback.
                frame = decode_frame(record["raw"])

            def last_check(*, check_snapshot=True):
                with self._lock:
                    if (self._halted or record["state"] != "REVIEWING"
                            or self._tick() >= frame["expires_tick"]
                            or self._binding() != record["owner"]
                            or self._policy.target(frame["path"]) != frame["path"]
                            or (check_snapshot and self._store.snapshot(frame["path"]) != record["before"])):
                        raise SourceConflict("review expired, cancelled or target/Owner changed")

            last_check()
            if self._owner.review(record["raw"], last_check) is not True:
                raise SourceConflict("Owner rejected source review")
            last_check()
            with self._lock:
                # Cancellation and the last pre-write check are serialized.
                # Store must invoke last_check under its atomic exclusion too.
                last_check()
                attempted = True
                after = self._store.commit(frame["path"], record["before"], record["data"], last_check)
                commit_returned = True
                if (type(after) is not SourceSnapshot or after.data != record["data"]
                        or after.identity is None or after.parents != record["before"].parents
                        or self._store.snapshot(frame["path"]) != after):
                    raise OSError("source integrity proof unconfirmed")
                completed_owner = self._binding()
                completed_tick = self._tick()
                if completed_owner != record["owner"] or completed_tick >= frame["expires_tick"]:
                    raise OSError("Owner/deadline lost after write")
                record["write_proof"] = SourceWriteProof(record["raw"], record["before"], after,
                                                         completed_tick)
                result = {"status": "CREATED_LOCAL_DEV" if frame["operation"] == "create" else "PATCHED_LOCAL_DEV",
                    "request_id": request_id, "path": frame["path"], "sha256": frame["after_sha256"],
                    "identity": list(after.identity), "review_sha256": sha256(record["raw"]).hexdigest(),
                    "nonce": frame["nonce"], "completed_tick": completed_tick}
        except BaseException as exc:
            # SourceConflict attests no mutation only while commit itself is
            # pending. Once commit returned, a later observation failure cannot
            # undo that write or justify reporting a harmless refusal.
            if attempted and (commit_returned or not isinstance(exc, SourceConflict)):
                self._halted = True
                result = {"status": "OUTCOME_UNKNOWN", "request_id": request_id,
                          "auto_retry": False, "auto_rollback": False, "recovery": "MANUAL_REVIEW_REQUIRED"}
            else:
                result = {"status": "DENIED", "request_id": request_id, "error": "SourceReviewRejected"}
            if not isinstance(exc, Exception):
                # Preserve terminal state even if local execution is interrupted.
                if record is not None:
                    with self._lock:
                        record["state"], record["result"] = result["status"], result
                raise
        finally:
            self._slot.release()
        with self._lock:
            if record is not None:
                record["state"], record["result"] = result["status"], result
            return decode_frame(encode_frame(result))
