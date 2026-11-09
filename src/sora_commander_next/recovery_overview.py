"""Read-only recovery guidance from existing JobManager session snapshots.

Never runs a command, grants authority, changes journal data or infers that a
missing END means nothing executed. Journal v1 itself has no request/argv
mapping; an existing Owner-private result archive can correlate request_id
to session_id without reconstructing executable, argv or Owner approval.
"""

_TERMINAL_SUCCESS = frozenset(("COMPLETED",))
_TERMINAL_DIAGNOSTIC = frozenset((
    "FAILED", "CANCELLED", "TIMEOUT", "OUTPUT_LIMIT",
    "OUTPUT_INCOMPLETE", "ERROR",
))


def _step(status, exit_code, output_incomplete):
    """Advisory next step, never executable permission."""
    if status == "RUNNING":
        return "OBSERVE_EXISTING_SESSION"
    if status == "COMPLETED" and exit_code == 0 and not output_incomplete:
        return "VERIFY_EFFECTS_THEN_CONTINUE"
    if status in _TERMINAL_SUCCESS | _TERMINAL_DIAGNOSTIC:
        return "INSPECT_RESULT_AND_EFFECTS"
    return "INSPECT_EFFECTS_MANUALLY"


def describe_recovery_sessions(previous, current, *, journal_enabled,
                               archive_enabled, current_request_ids=None,
                               previous_archive_rows=None):
    """Transform already-observed snapshots into bounded, inert guidance."""
    if type(previous) is not dict or type(current) is not dict:
        raise ValueError("invalid session snapshots")
    old = previous["sessions"]
    live = current["sessions"]
    if type(old) is not list or type(live) is not list or len(old) > 256 or len(live) > 256:
        raise ValueError("unbounded session snapshots")
    mapping = current_request_ids if current_request_ids is not None else {}
    if type(mapping) is not dict:
        raise ValueError("invalid session correlation")
    archive = previous_archive_rows if previous_archive_rows is not None else []
    if (type(archive) is not list or len(archive) > 256
            or any(type(item) is not dict for item in archive)):
        raise ValueError("invalid archive correlation")
    archive_by_session = {}
    for item in archive:
        sid = item.get("session_id")
        rid = item.get("request_id")
        if (type(sid) is not str or len(sid) != 48
                or any(c not in "0123456789abcdef" for c in sid)
                or type(rid) is not str or not 1 <= len(rid) <= 128
                or "\0" in rid or sid in archive_by_session):
            raise ValueError("invalid archive identity")
        archive_by_session[sid] = item
    rows = []
    known_previous = set()
    for origin, entries in (("PREVIOUS_PROCESS", old), ("CURRENT_PROCESS", live)):
        for item in entries:
            # These records contain only the six fields of session_journal v1.
            status = item["status"]
            row = {
                "session_id": item["session_id"],
                "origin": origin,
                "status": status,
                "exit_code": item["exit_code"],
                "output_incomplete": item["output_incomplete"],
                "next_step": _step(
                    status, item["exit_code"], item["output_incomplete"]),
                "auto_retry": False,
                "command_identified": False,
                "task_acceptance_verified": False,
                "live_supervision_available": origin == "CURRENT_PROCESS",
            }
            if origin == "CURRENT_PROCESS":
                request_id = mapping.get(item["session_id"])
                if request_id is not None:
                    row["request_id"] = request_id
            else:
                known_previous.add(item["session_id"])
                archived = archive_by_session.get(item["session_id"])
                if archived is not None:
                    # The Owner-private output archive already records the
                    # original request ↔ session link. Never expose chunks.
                    row["request_id"] = archived["request_id"]
                    row["archive_status"] = archived["status"]
                    row["archive_exit_code"] = archived["exit_code"]
                    row["archive_saved_at"] = archived["saved_at"]
                    row["correlation"] = "EXISTING_ARCHIVE_REQUEST_ID"
                    if "safe_command" in archived:
                        from .safe_command_identity import display_safe_command
                        line = display_safe_command(archived["safe_command"])
                        if line is not None:
                            row["safe_command"] = {
                                "executable": archived["safe_command"]["executable"],
                                "argv": list(archived["safe_command"]["argv"]),
                                "cwd": archived["safe_command"]["cwd"],
                            }
                            row["command_line"] = line
                            row["command_identified"] = True
                    # A stale partial archive is not contradictory; genuinely
                    # different terminal records require explicit inspection.
                    if (status not in ("INTERRUPTED_UNKNOWN", "RUNNING")
                            and archived["status"] not in ("INTERRUPTED_UNKNOWN", "RUNNING")
                            and (archived["status"] != status
                                 or archived["exit_code"] != item["exit_code"])):
                        row["evidence_conflict"] = True
                        row["next_step"] = "INSPECT_EFFECTS_MANUALLY"
            rows.append(row)
    for sid, archived in archive_by_session.items():
        if sid not in known_previous:
            # Archived checkpoint with no journal counterpart is evidence
            # of a request ID, NOT proof of completion or no side effects.
            row = {
                "session_id": sid, "origin": "ARCHIVE_ONLY",
                "status": "OUTCOME_UNKNOWN", "exit_code": None,
                "output_incomplete": True,
                "request_id": archived["request_id"],
                "archive_status": archived["status"],
                "archive_exit_code": archived["exit_code"],
                "archive_saved_at": archived["saved_at"],
                "correlation": "EXISTING_ARCHIVE_REQUEST_ID",
                "next_step": "INSPECT_EFFECTS_MANUALLY",
                "auto_retry": False, "command_identified": False,
                "task_acceptance_verified": False,
                "live_supervision_available": False,
            }
            if "safe_command" in archived:
                from .safe_command_identity import display_safe_command
                line = display_safe_command(archived["safe_command"])
                if line is not None:
                    row["safe_command"] = {
                        "executable": archived["safe_command"]["executable"],
                        "argv": list(archived["safe_command"]["argv"]),
                        "cwd": archived["safe_command"]["cwd"],
                    }
                    row["command_line"] = line
                    row["command_identified"] = True
            rows.append(row)
    return {
        "status": "OK",
        "mode": "READ_ONLY_MANUAL_RECOVERY",
        "journal": "ENABLED" if journal_enabled else "DISABLED",
        "output_archive": "ENABLED" if archive_enabled else "DISABLED",
        "auto_retry": False,
        "after_host_restart": "CURRENT_MANAGER_IDS_UNAVAILABLE_UNLESS_INDEPENDENTLY_ANCHORED",
        "historical_end": "ADVISORY_NOT_LIVE_SUPERVISION_OR_TASK_ACCEPTANCE",
        "sessions": rows,
        "count": len(rows),
        "identification_limit": (
            "ALLOWLISTED_SAFE_FULL_COMMAND_ONLY"
            if any(row["command_identified"] for row in rows)
            else "ARCHIVE_REQUEST_ID_CORRELATION_ONLY"
            if archive_by_session else "PREVIOUS_SESSION_ID_ONLY"),
        "note": ("Missing END or missing session does not prove a command did "
                 "not execute. Inspect logs, command context and side effects "
                 "before submitting a distinct new Owner-approved operation."),
    }
