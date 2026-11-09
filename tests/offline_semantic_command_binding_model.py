"""DEV_ONLY lexical semantic-command binding evidence; NEVER an authority.

A command binding intentionally omits request_id/timeout/output_limit so it
can identify the same immutable executable/argv/cwd across separate attempts.
This is NOT Windows object identity: junction/8.3/hardlink, mutable file contents,
environment, DLL, current program behavior and approval intent are unverified.
The caller can create arbitrary Commands and bindings; no service/Owner proof.
No filesystem writes, clock, subprocess, OS token, broker or ledger integration.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re

from sora_commander_next.commands import Command

_DOMAIN = b"sora-commander-next/offline-semantic-command/lexical-v1\0"


@dataclass(frozen=True, slots=True)
class SemanticBindingEvidence:
    digest: str
    scope: str = field(default="LEXICAL_ONLY_DATA_ONLY", init=False)
    authority: str = field(default="NOT_AUTHENTICATED", init=False)
    admission: str = field(default="UNAVAILABLE", init=False)
    os_acceptance: str = field(default="NOT_OS_ACCEPTED", init=False)


def _verify_command(command):
    if type(command) is not Command:
        raise ValueError("EXACT_COMMAND_TYPE_REQUIRED")
    if (type(command.request_id) is not str or
            re.fullmatch(r"[0-9a-f]{32}", command.request_id) is None):
        raise ValueError("INVALID_REQUEST_ID")
    for name in ("executable", "cwd"):
        value = getattr(command, name)
        if (type(value) is not str or not value or "\0" in value
                or not Path(value).is_absolute()):
            raise ValueError("INVALID_COMMAND_PATH")
    if (type(command.argv) is not tuple or len(command.argv) > 128
            or any(type(a) is not str or "\0" in a or len(a) > 8192
                   for a in command.argv)):
        raise ValueError("INVALID_COMMAND_ARGV")
    try:
        time_ok = (type(command.timeout) in (int, float)
                   and math.isfinite(command.timeout)
                   and 0.01 <= command.timeout <= 3600)
    except (TypeError, OverflowError, ValueError):
        time_ok = False
    if (not time_ok or type(command.output_limit) is not int
            or not 128 <= command.output_limit <= 65536):
        raise ValueError("INVALID_COMMAND_LIMITS")


def derive_semantic_binding_data_only(command: Command) -> SemanticBindingEvidence:
    """Deterministic byte framing for *lexical* equivalence, never OS identity."""
    _verify_command(command)
    frame = {
        "kind": "lexical-command-v1",
        "executable": os.path.normcase(os.path.normpath(command.executable)),
        "cwd": os.path.normcase(os.path.normpath(command.cwd)),
        "argv": command.argv,
    }
    raw = json.dumps(frame, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True, allow_nan=False).encode("ascii")
    return SemanticBindingEvidence(sha256(_DOMAIN + raw).hexdigest())


def compare_claimed_binding_data_only(command, claimed) -> tuple[bool, str]:
    """Claim matching cannot prove identity, owner approval or replay safety."""
    evidence = derive_semantic_binding_data_only(command)
    if (type(claimed) is not str or
            re.fullmatch(r"[0-9a-f]{64}", claimed) is None):
        return False, "NOT_AUTHENTICATED"
    return claimed == evidence.digest, "NOT_AUTHENTICATED"
