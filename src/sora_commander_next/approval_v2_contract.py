"""Approval v2: lightweight surface-command comparison, DATA ONLY.

The command boundary is executable path + exact argv + cwd + Owner/device and
interaction mode. Script contents, descendants, libraries, configuration files
and program effects are deliberately outside the approval fingerprint.

This module cannot attest Owner consent, authenticate a machine, issue grants,
persist revocations or authorize a process. Only a separate trusted local
broker may do that, after applying mandatory OS/policy denial rules.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import PureWindowsPath
import re

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_DOMAIN = b"SoraCommanderNext/approval-v2/surface-command/v3\0"
_MODES = frozenset(("INTERACTIVE_ONLY", "UNATTENDED_EXPLICIT"))
_CHOICES = frozenset(("ONCE", "TRUSTED", "DENY"))


def _sha(value):
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError("lowercase SHA-256 required")
    return value


def _id(value):
    if type(value) is not str or not 1 <= len(value) <= 256 or "\0" in value:
        raise ValueError("nonempty bounded identity required")
    return value


def _local_path(value):
    """Canonical lexical Windows spelling only. Not OS file authentication."""
    if type(value) is not str or not 4 <= len(value) <= 1024 or "\0" in value:
        raise ValueError("invalid Windows path")
    p = PureWindowsPath(value)
    if (not p.is_absolute() or len(p.drive) != 2 or p.drive[1] != ":"
            or str(p) != value):
        raise ValueError("absolute canonical local drive path required")
    if any(part in (".", "..") or part.endswith((" ", ".")) or ":" in part
           for part in p.parts[1:]):
        raise ValueError("ambiguous Windows path")
    return value


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """What command the Owner is asked to allow, not what the program does."""

    kind: str
    owner_id: str
    machine_id: str
    executable: str
    argv: tuple[str, ...]
    cwd: str
    session_class: str = "INTERACTIVE_ONLY"

    def __post_init__(self):
        if type(self.kind) is not str or self.kind != "RUN_COMMAND":
            raise ValueError("unsupported operation kind")
        _id(self.owner_id)
        _id(self.machine_id)
        _local_path(self.executable)
        _local_path(self.cwd)
        if type(self.session_class) is not str or self.session_class not in _MODES:
            raise ValueError("explicit session mode required")
        if (type(self.argv) is not tuple or len(self.argv) > 128
                or any(type(arg) is not str or "\0" in arg or len(arg) > 8192
                       for arg in self.argv)):
            raise ValueError("exact argv required")

    @property
    def digest(self):
        obj = {
            "kind": self.kind, "owner_id": self.owner_id,
            "machine_id": self.machine_id, "executable": self.executable,
            "argv": self.argv, "cwd": self.cwd, "session_class": self.session_class,
        }
        canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True).encode("ascii")
        return sha256(_DOMAIN + canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    """An inert claimed decision: true permission requires trusted broker state."""

    approval_id: str
    spec_sha256: str
    owner_id: str
    choice: str
    review_sha256: str
    owner_evidence_sha256: str
    revision: int
    max_timeout_ms: int
    max_output_limit: int
    revoked: bool = False
    spent: bool = False

    def __post_init__(self):
        for part in (self.approval_id, self.spec_sha256, self.review_sha256,
                     self.owner_evidence_sha256):
            _sha(part)
        _id(self.owner_id)
        if type(self.choice) is not str or self.choice not in _CHOICES:
            raise ValueError("explicit Owner choice required")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("valid revision required")
        if type(self.max_timeout_ms) is not int or not 10 <= self.max_timeout_ms <= 3_600_000:
            raise ValueError("bounded timeout required")
        if type(self.max_output_limit) is not int or not 128 <= self.max_output_limit <= 65536:
            raise ValueError("bounded output limit required")
        if type(self.revoked) is not bool or type(self.spent) is not bool:
            raise ValueError("boolean state required")


def operation_for_command(command, *, owner_id, machine_id,
                          session_class="INTERACTIVE_ONLY"):
    """Capture the validated surface of an existing Command (DATA ONLY).

    Owner and machine identity MUST come from the trusted local side, never
    from MCP input. This mapper is not an Owner verifier or authorization.
    """
    from .commands import Command
    if type(command) is not Command:
        raise TypeError("validated Command required")
    return OperationSpec(
        kind="RUN_COMMAND", owner_id=owner_id, machine_id=machine_id,
        executable=command.executable, argv=command.argv, cwd=command.cwd,
        session_class=session_class)


def compare_claim(consent, current, *, timeout_ms, output_limit):
    """Cheap equality check. MATCHING_DATA_ONLY must NEVER dispatch a job.

    Executable content and script dependencies are intentionally not inspected.
    The actual broker must reject unapproved requests, invalid Owner sessions,
    forbidden roots/elevation and duplicate/revoked operations independently.
    """
    if type(consent) is not ConsentRecord or type(current) is not OperationSpec:
        return "DENY"
    if consent.revoked or consent.choice == "DENY" or (
            consent.choice == "ONCE" and consent.spent):
        return "DENY"
    if (type(timeout_ms) is not int or type(output_limit) is not int
            or timeout_ms < 10 or output_limit < 128):
        return "DENY"
    if consent.owner_id != current.owner_id or consent.spec_sha256 != current.digest:
        return "REVIEW"
    if timeout_ms > consent.max_timeout_ms or output_limit > consent.max_output_limit:
        return "REVIEW"
    return "MATCHING_DATA_ONLY"
