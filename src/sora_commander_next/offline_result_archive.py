"""Data-only, opt-in result archive for reboot-time manual log recovery.

Not an Owner grant, execution ledger, identity authenticator or job replay
mechanism. May contain SECRET output: the caller MUST independently verify
the Owner-private directory and ACL. JobManager writes only on explicit
result-archive opt-in; read-only Host/MCP retrieval cannot start a job.
"""
import base64
import binascii
import json
import os
from pathlib import Path
import stat
import tempfile
import time

MAX_RECORDS = 32
MAX_CAPTURE = 8192
MAX_ARCHIVE_SIZE = 512 * 1024
MAX_AGE_SECONDS = 7 * 86400
STATES = frozenset({
    "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
    "OUTPUT_LIMIT", "OUTPUT_INCOMPLETE", "ERROR", "OUTCOME_UNKNOWN",
})
_TERMINAL = STATES - {"RUNNING"}


class ResultArchiveError(ValueError):
    pass


def _directory(path):
    p = Path(path)
    parent = p.parent
    if (not p.is_absolute() or p.is_symlink() or p.is_junction()
            or not parent.is_dir() or parent.is_symlink()
            or parent.is_junction() or p.name != "owner-output-archive.json"):
        raise ResultArchiveError("unsupported Owner archive path")
    if p.exists():
        info = p.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ResultArchiveError("invalid Owner archive file")
    return p


def _record(entry):
    """Validate output data precisely. Never execute, log or inspect argv."""
    expected = {"request_id", "session_id", "status", "exit_code",
                "captured_bytes", "output_incomplete", "saved_at",
                "chunks", "archived_truncated"}
    if (type(entry) is not dict
            or set(entry) not in (expected, expected | {"safe_command"})):
        raise ResultArchiveError("invalid result fields")
    if "safe_command" in entry:
        from .safe_command_identity import display_safe_command
        if display_safe_command(entry["safe_command"]) is None:
            raise ResultArchiveError("unsafe archived command identity")
    request = entry["request_id"]
    session = entry["session_id"]
    if (type(request) is not str or not 1 <= len(request) <= 128
            or "\0" in request or type(session) is not str
            or len(session) != 48
            or any(c not in "0123456789abcdef" for c in session)):
        raise ResultArchiveError("invalid result identity")
    state = entry["status"]
    code = entry["exit_code"]
    if (type(state) is not str or state not in STATES
            or (code is not None and (type(code) is not int
                                      or not -2**31 <= code <= 2**32-1))
            or type(entry["captured_bytes"]) is not int
            or not 0 <= entry["captured_bytes"] <= 65536
            or type(entry["output_incomplete"]) is not bool
            or type(entry["archived_truncated"]) is not bool
            or type(entry["saved_at"]) is not int
            or not 0 < entry["saved_at"] < 2**63):
        raise ResultArchiveError("invalid result metadata")
    if state == "RUNNING" and code is not None:
        raise ResultArchiveError("running job cannot have an exit code")
    # Recorded success requires an observed zero process exit. If the final
    # poll was ambiguous, retain the earlier RUNNING checkpoint for UNKNOWN
    # recovery rather than persisting an unproven COMPLETED result.
    if state == "COMPLETED" and code != 0:
        raise ResultArchiveError("completed job requires observed exit zero")
    pieces = entry["chunks"]
    if type(pieces) is not list or len(pieces) > 128:
        raise ResultArchiveError("invalid output chunks")
    total = 0
    previous_end = 0
    clean = []
    for piece in pieces:
        if (type(piece) is not dict
                or set(piece) != {"offset", "stream", "data_base64"}
                or type(piece["offset"]) is not int
                or piece["offset"] != previous_end
                or piece["stream"] not in ("stdout", "stderr")
                or type(piece["data_base64"]) is not str):
            raise ResultArchiveError("invalid output chunk")
        try:
            raw = base64.b64decode(piece["data_base64"], validate=True)
        except (ValueError, binascii.Error):
            raise ResultArchiveError("invalid encoded result") from None
        if not raw or total + len(raw) > MAX_CAPTURE:
            raise ResultArchiveError("oversized output archive")
        total += len(raw)
        previous_end = total
        clean.append({"offset": piece["offset"], "stream": piece["stream"],
                      "data_base64": piece["data_base64"]})
    if total > entry["captured_bytes"]:
        raise ResultArchiveError("archived bytes exceed captured bytes")
    if total < entry["captured_bytes"] and not entry["archived_truncated"]:
        raise ResultArchiveError("missing truncation marker")
    return {**{name: entry[name] for name in expected if name != "chunks"},
            "chunks": clean,
            **({"safe_command": {
                "executable": entry["safe_command"]["executable"],
                "argv": list(entry["safe_command"]["argv"]),
                "cwd": entry["safe_command"]["cwd"],
            }} if "safe_command" in entry else {})}


def _object_pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ResultArchiveError("duplicate archive key")
        out[k] = v
    return out


def _load(path):
    p = _directory(path)
    if not p.exists():
        return []
    if p.stat().st_size > MAX_ARCHIVE_SIZE:
        raise ResultArchiveError("archive too large")
    try:
        with p.open("rb") as f:
            raw = f.read(MAX_ARCHIVE_SIZE + 1)
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs)
    except (UnicodeError, OSError, ValueError) as exc:
        raise ResultArchiveError("archive unavailable") from exc
    if (type(obj) is not dict or set(obj) != {"version", "results"}
            or type(obj["version"]) is not int or obj["version"] != 1
            or type(obj["results"]) is not list
            or len(obj["results"]) > MAX_RECORDS):
        raise ResultArchiveError("invalid archive structure")
    items = [_record(r) for r in obj["results"]]
    if len({r["session_id"] for r in items}) != len(items):
        raise ResultArchiveError("duplicate result sessions")
    return items


def _archived_output_pairs(chunks):
    """Stream-labelled bytes; ignore chunk segmentation, not byte order."""
    for chunk in chunks:
        for value in base64.b64decode(chunk["data_base64"], validate=True):
            yield (chunk["stream"], value)


def write_result_checkpoint(path, entry, *, now=None, protected_stage=False):
    """Atomic, bounded replace in a pre-verified Owner-private directory.

    Does NOT assign or inspect Windows ACLs; callers must independently
    validate Owner-only storage. Every call is one complete checkpoint, not
    an approval receipt. Writers must be serialized by the owning Broker.
    """
    if now is None:
        now = int(time.time())
    if type(now) is not int or now <= 0:
        raise ResultArchiveError("invalid clock")
    p = _directory(path)
    item = _record(entry)
    if item["saved_at"] > now + 60:
        raise ResultArchiveError("future result timestamp")
    existing = _load(p)
    previous = next((old for old in existing
                     if old["session_id"] == item["session_id"]), None)
    if previous is not None:
        if (previous["request_id"] != item["request_id"]
                or previous.get("safe_command") != item.get("safe_command")
                or previous["saved_at"] > item["saved_at"]
                or previous["captured_bytes"] > item["captured_bytes"]
                or (previous["status"] in _TERMINAL
                    and item["status"] != previous["status"])):
            raise ResultArchiveError("stale or conflicting result checkpoint")
        # Once captured as recovery evidence, prior stdout/stderr bytes and
        # their stream attribution are immutable. Later checkpoints may
        # append data or coalesce chunks, but must preserve the exact
        # stream-labelled prefix even if archived_truncated was already true.
        current_pairs = iter(_archived_output_pairs(item["chunks"]))
        if any(next(current_pairs, None) != old_pair
               for old_pair in _archived_output_pairs(previous["chunks"])):
            raise ResultArchiveError("archived output prefix changed")
    history = [
        old for old in existing
        if now - old["saved_at"] <= MAX_AGE_SECONDS
        and old["session_id"] != item["session_id"]
    ]
    history.append(item)
    history.sort(key=lambda e: (e["saved_at"], e["session_id"]))
    history = history[-MAX_RECORDS:]
    encoded = json.dumps(
        {"version": 1, "results": history},
        separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
    if len(encoded) > MAX_ARCHIVE_SIZE:
        raise ResultArchiveError("archive capacity exceeded")
    # A same-directory temp file inherits that directory's ACL.
    # This is NOT a substitute for verifying the Owner-private ACL.
    temp = None
    parent = p.parent
    identity = (parent.stat().st_dev, parent.stat().st_ino)
    old = p.stat() if p.exists() else None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=parent, prefix=".sora-result-",
            suffix=".tmp", delete=False,
        ) as out:
            temp = Path(out.name)
            out.write(encoded)
            out.flush()
            os.fsync(out.fileno())
        staged = temp.lstat()
        if (not stat.S_ISREG(staged.st_mode) or staged.st_nlink != 1
                or parent.is_symlink() or parent.is_junction()
                or (parent.stat().st_dev, parent.stat().st_ino) != identity
                or p.is_symlink() or p.is_junction()):
            raise ResultArchiveError("archive path changed")
        if old is None and p.exists():
            raise ResultArchiveError("archive target appeared")
        if old is not None:
            if not p.exists():
                raise ResultArchiveError("archive target disappeared")
            fresh = p.stat()
            if (fresh.st_dev, fresh.st_ino) != (old.st_dev, old.st_ino):
                raise ResultArchiveError("archive target changed")
        if protected_stage:
            from .owner_stage_acl import protect_owner_stage_local
            protect_owner_stage_local(temp)
        os.replace(temp, p)
        if protected_stage:
            from .owner_stage_acl import verify_owner_stage_after_replace
            verify_owner_stage_after_replace(p)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def read_previous_results(path, *, now=None):
    """Return a detached read-only DATA copy; NEVER resume prior jobs."""
    if now is None:
        now = int(time.time())
    if type(now) is not int or now <= 0:
        raise ResultArchiveError("invalid clock")
    rows = []
    for item in _load(path):
        # A future-dated archive is not proof that a job has completed.
        # The writer applies the same 60-second tolerance; clock rollback or
        # tampered records require manual review rather than implicit trust.
        if item["saved_at"] > now + 60:
            raise ResultArchiveError("future result timestamp")
        if now - item["saved_at"] > MAX_AGE_SECONDS:
            continue
        state = ("INTERRUPTED_UNKNOWN" if item["status"] == "RUNNING"
                 else item["status"])
        result = {**item, "status": state}
        if state == "INTERRUPTED_UNKNOWN":
            result["exit_code"] = None
            result["output_incomplete"] = True
        result["chunks"] = [dict(c) for c in item["chunks"]]
        rows.append(result)
    return {"results": rows, "count": len(rows)}
