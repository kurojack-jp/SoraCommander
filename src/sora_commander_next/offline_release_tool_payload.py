"""DEV_ONLY in-memory release helper payload audit; always HOLD_LIVE.

The current v1 candidate inventory copies Python and optional icon ONLY.
This module prepares exact script bytes for a future, separately signed
manifest and protected installer. A caller-provided hash is NOT a trusted
publisher anchor, and no filesystem/Win32/execution action is implemented.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re

HELPERS = (
    "Invoke-NextCodexJob.ps1",
    "cli-monitor.ps1",
    "start-cli-monitor.vbs",
)
MAX_HELPER_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 512 * 1024
FORMAT = "scn-tools-dev-manifest-v1"
_HASH = re.compile(r"[0-9a-f]{64}")
_DRIVE = re.compile(r"(?i)(?<![a-z0-9_])[a-z]:[\\/]")
_DEVICE = re.compile(r"\\\\[?.]\\")
_BY_PASS = "--dangerously-bypass-approvals-and-sandbox"


class ToolPayloadError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ToolPayloadReview:
    manifest_bytes: bytes
    manifest_sha256: str
    file_sha256: tuple
    file_count: int
    literal_path_scan_passed: bool = field(default=True, init=False)
    independent_signature_verified: bool = field(default=False, init=False)
    windows_acl_verified: bool = field(default=False, init=False)
    dependency_runtime_verified: bool = field(default=False, init=False)
    release_permitted: bool = field(default=False, init=False)
    spawn_permitted: bool = field(default=False, init=False)
    status: str = field(default="HOLD_LIVE", init=False)


def audit_tool_payload_bytes(payloads, expected_sha256):
    """Reject incomplete, unpinned, nonportable script payloads, in memory.

    expected_sha256 is an exact externally supplied table, NOT a signature.
    Even a perfect comparison provides no publisher, OS ACL, dependency
    integrity, installer provenance, or Broker launch authorization.
    """
    if type(payloads) is not dict or type(expected_sha256) is not dict:
        raise ToolPayloadError("exact byte map and caller hash map required")
    if set(payloads) != set(HELPERS) or set(expected_sha256) != set(HELPERS):
        raise ToolPayloadError("complete exact reviewed helper inventory required")
    records = []
    total = 0
    for name in HELPERS:
        data, pinned = payloads[name], expected_sha256[name]
        if type(data) is not bytes or not 0 < len(data) <= MAX_HELPER_BYTES:
            raise ToolPayloadError("ordinary bounded script bytes required")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ToolPayloadError("helper bundle exceeds total byte limit")
        if type(pinned) is not str or _HASH.fullmatch(pinned) is None:
            raise ToolPayloadError("strict caller-pinned sha256 required")
        digest = sha256(data).hexdigest()
        if digest != pinned:
            raise ToolPayloadError("script bytes differ from caller pins")
        try:
            source = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolPayloadError("UTF-8 helper source required") from exc
        if ("\0" in source or source.startswith("\ufeff")
                or "\r" in source.replace("\r\n", "")
                or _DEVICE.search(source) or _DRIVE.search(source)
                or "\\\\" in source):
            raise ToolPayloadError("ambiguous or hardcoded absolute helper path")
        if _BY_PASS in source.casefold():
            raise ToolPayloadError("unsafe sandbox bypass option in script")
        records.append({"name": name, "bytes": len(data), "sha256": digest})
    raw = (json.dumps({"format": FORMAT, "files": records},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)
           + "\n").encode("ascii")
    return ToolPayloadReview(raw, sha256(raw).hexdigest(),
                             tuple((item["name"], item["sha256"]) for item in records),
                             len(records))
