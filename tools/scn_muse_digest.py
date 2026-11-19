"""Read-only digest for the Muse bridge's own checkout and registry."""

import json
import re
from typing import Annotated, Any

from pydantic import Field

from sora_commander_next import activity, git_readonly
from sora_commander_next.file_reads import ReadBoundaryError
from sora_commander_next.mcp_adapter import _local_registered_catalog


_MAX_BYTES = 16 * 1024
_LOG_DATE = re.compile(r"(?<=\t)\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d(?=\t)")


def _git_summary(checkout, roots):
    try:
        snapshot = git_readonly.git_snapshot(checkout, roots)
        log = git_readonly.git_log(checkout, roots, limit=3)
        lines = snapshot["status"].split("\n")
        headers = dict(line[2:].split(" ", 1) for line in lines if line.startswith("# "))
        commits = []
        for line in log["content"].split("\n"):
            if line:
                sha, _, text = line.partition("\t")
                # A literal tab can occur in an author or subject. If the date
                # boundary is ambiguous, do not expose any part of the author.
                date, = _LOG_DATE.finditer(text)
                commits.append({"sha": sha, "date": date[0], "subject": text[date.end() + 1:]})
        return {
            "branch": headers["branch.head"],
            "head": headers["branch.oid"],
            "changed": sum(line.startswith(("1 ", "2 ", "u ")) for line in lines),
            "untracked": snapshot["untracked_count"],
            "log": commits,
        }
    except ReadBoundaryError as exc:
        code = str(exc)
        return {"error": code if code in git_readonly.REASON_CODES else "PATH_NOT_ALLOWED"}
    except Exception:
        return {"error": "GIT_FAILED"}


def _recent_activity(host, limit):
    if limit == 0:
        return []
    try:
        result = activity.observe(host, {"mode": "recent", "limit": limit})
        if result["status"] == "OK":
            return list(result["records"])
    except Exception:
        pass
    return {"error": "UNAVAILABLE"}


def _cap(reply):
    def too_large():
        return len(json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _MAX_BYTES

    if too_large():
        reply["truncated"] = True
        for commit in reply["git"].get("log", ()):
            commit["subject"] = commit["subject"][:80]
        records = reply["activity"]
        if isinstance(records, list):
            while records and too_large():
                records.pop()
        if too_large() and "branch" in reply["git"]:
            # An unusually long ref can exhaust the cap on its own.
            reply["git"]["branch"] = reply["git"]["branch"][:80]
    return reply


def register_digest(server, host, *, checkout, auth, inbox):
    """Register the digest after composing the readonly server."""

    @server.tool()
    def bridge_digest(
        activity_limit: Annotated[int, Field(
            strict=True, ge=0, le=20,
            description="Maximum recent activity records, strict integer 0 to 20. Zero skips the activity read.",
        )] = 5,
    ) -> dict[str, Any]:
        """Read bridge status, the checkout's Git summary and recent activity. The reply is capped at 16 KiB and marks size reductions with truncated. Report any git or activity error without retrying."""
        catalog = _local_registered_catalog(server, host)
        return _cap({
            "status": {
                "version": catalog["installed_distribution_version"],
                "mode": "readonly",
                "auth": auth,
                "inbox": inbox,
                "registered_tool_count": catalog["registered_tool_count"],
                "tool_schema_sha256": catalog["tool_schema_sha256"],
                "remote_ready": host.status()["remote_ready"],
            },
            "git": _git_summary(str(checkout), host.policy.read_roots),
            "activity": _recent_activity(host, activity_limit),
            "truncated": False,
        })
