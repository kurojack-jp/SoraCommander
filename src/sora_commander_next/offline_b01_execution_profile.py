"""U2-c strict execution-profile recipe, DEV_ONLY DATA ONLY.

Everything, including Owner/token/environment/Publisher evidence, is ASSERTED
by the caller. This module never reads the OS, builds a token or environment
block for CreateProcess, opens a file, grants authority or starts a job.
A live protected Broker MUST independently establish every declared fact.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import re
import unicodedata

from .offline_b01_full_binding import (
    ContractError, FrozenCommand, _assert_keys, _canonical, _decode,
    _hex, _HEX32, _HEX64, _number, _sid, _text, _windows_local_path,
    freeze_command,
)

DOMAIN = b"sora-commander-next/execution-profile/v1\x00"
MAX_PROFILE_BYTES = 32768
MAX_ENV_BYTES = 16384  # strict bound on both JSON and future UTF-16LE block
_FIELDS = frozenset((
    "format_version", "profile_id", "installation_id", "generation",
    "release_sequence", "policy_generation", "policy_sha256", "owner_sid",
    "publisher_key_sha256", "executable", "executable_sha256", "cwd",
    "read_roots", "write_roots", "environment", "token", "job",
    "network_mode", "allow_path_search", "inherit_parent_environment",
))
_TOKEN = frozenset((
    "origin", "kind", "integrity", "restricted_sids", "deny_only_sids",
    "disabled_privileges", "allow_elevation",
))
_JOB = frozenset((
    "launch_mode", "kill_on_close", "breakaway_allowed",
    "max_processes", "max_runtime_ms", "max_output_bytes",
    "memory_limit_bytes", "detach_allowed",
))
_ENV_ITEM = frozenset(("name", "value"))
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}\Z")
_PRIVILEGE = re.compile(r"Se[A-Za-z]{2,64}Privilege\Z")
_KNOWN_ENV_INTERPRETERS = frozenset((
    "COMSPEC", "PATHEXT", "PROMPT", "PSMODULEPATH", "PYTHONPATH", "NODE_OPTIONS",
    "PYTHONSTARTUP", "PYTHONHOME", "RUBYOPT", "LD_PRELOAD",
))


def _boolean(value, name, expected=None):
    if type(value) is not bool or (expected is not None and value is not expected):
        raise ContractError("invalid " + name)


def _sorted_unique(items, *, name, max_items, check):
    if type(items) is not list or len(items) > max_items:
        raise ContractError("invalid " + name)
    for item in items:
        check(item)
    if items != sorted(items) or len(set(items)) != len(items):
        raise ContractError("duplicate or unsorted " + name)


def _path_roots(data):
    for name in ("read_roots", "write_roots"):
        _sorted_unique(data[name], name=name, max_items=16,
                       check=_windows_local_path)
    if not data["read_roots"] or not data["write_roots"]:
        raise ContractError("explicit scoped roots required")
    # No implicit root broadening by case-variant or ancestor-based aliases.
    for name in ("read_roots", "write_roots"):
        folded = [p.casefold() for p in data[name]]
        if len(set(folded)) != len(folded):
            raise ContractError("case-alias root")
    # Source root and destination must be established by OS after this.
    cwd = data["cwd"].casefold().rstrip("\\")
    if not any(cwd == root.casefold().rstrip("\\") or
               cwd.startswith(root.casefold().rstrip("\\") + "\\")
               for root in data["read_roots"]):
        raise ContractError("cwd outside asserted read roots")
    for path in data["write_roots"]:
        if not any(path.casefold().rstrip("\\") == r.casefold().rstrip("\\") or
                   path.casefold().startswith(r.casefold().rstrip("\\") + "\\")
                   for r in data["read_roots"]):
            raise ContractError("write root must be inside a read root")


def _environment(data):
    entries = data["environment"]
    if type(entries) is not list or len(entries) > 32:
        raise ContractError("explicit environment count required")
    observed = []
    for entry in entries:
        _assert_keys(entry, _ENV_ITEM)
        name, value = entry["name"], entry["value"]
        if type(name) is not str or _ENV_NAME.fullmatch(name) is None:
            raise ContractError("canonical uppercase environment name required")
        if name in _KNOWN_ENV_INTERPRETERS or name.endswith(("_TOKEN", "_PASSWORD", "_SECRET", "_KEY")):
            raise ContractError("unsafe environment name requires separate review")
        _text(value, 2048, "environment value")
        if any(unicodedata.category(char) in ("Cc", "Cf") for char in value):
            raise ContractError("invisible/control environment character")
        if name == "PATH":
            segments = value.split(";")
            if not 1 <= len(segments) <= 8:
                raise ContractError("bounded explicit PATH segments required")
            for segment in segments:
                _windows_local_path(segment)
            if len({segment.casefold() for segment in segments}) != len(segments):
                raise ContractError("duplicate PATH entries")
        if name in ("SYSTEMROOT", "TEMP", "TMP"):
            _windows_local_path(value)
        observed.append(name)
    if observed != sorted(observed) or len(set(observed)) != len(observed):
        raise ContractError("environment names must be sorted and unique")
    if len(_canonical(entries, MAX_PROFILE_BYTES)) > MAX_ENV_BYTES:
        raise ContractError("environment JSON too large")
    # CreateProcess Windows environment format is a UTF-16LE string sequence
    # of NAME=value\0 pairs terminated by a second NUL. This only checks
    # proposed bytes; no native block is constructed or passed to the OS.
    utf16_bytes = 2 + sum(
        len((entry["name"] + "=" + entry["value"] + "\0").encode("utf-16-le"))
        for entry in entries
    )
    if utf16_bytes > MAX_ENV_BYTES:
        raise ContractError("proposed Windows environment block too large")
    if not entries:
        raise ContractError("environment block must be explicitly populated")


def _token(data):
    token = data["token"]
    _assert_keys(token, _TOKEN)
    if token["origin"] != "INTERACTIVE_OWNER_TOKEN" or type(token["origin"]) is not str:
        raise ContractError("token origin must be explicit Owner token")
    if token["kind"] != "RESTRICTED_PRIMARY" or type(token["kind"]) is not str:
        raise ContractError("restricted primary token recipe required")
    if type(token["integrity"]) is not str or token["integrity"] not in ("LOW", "MEDIUM"):
        raise ContractError("unsupported token integrity")
    for key in ("restricted_sids", "deny_only_sids"):
        _sorted_unique(token[key], name=key, max_items=16, check=_sid)
    if not token["restricted_sids"]:
        raise ContractError("restricted SID list is required")
    _sorted_unique(token["disabled_privileges"], name="disabled privileges",
                   max_items=32, check=lambda x: _text(x, 96, "privilege"))
    for privilege in token["disabled_privileges"]:
        if _PRIVILEGE.fullmatch(privilege) is None:
            raise ContractError("invalid privilege name")
    _boolean(token["allow_elevation"], "token elevation", False)


def _job(data):
    job = data["job"]
    _assert_keys(job, _JOB)
    if type(job["launch_mode"]) is not str or job["launch_mode"] != "SUSPENDED_BEFORE_JOB_ASSIGN":
        raise ContractError("suspended Job assignment required")
    _boolean(job["kill_on_close"], "job kill on close", True)
    _boolean(job["breakaway_allowed"], "job breakaway", False)
    _boolean(job["detach_allowed"], "detached observation")
    _number(job["max_processes"], 1, 32, "max job processes")
    _number(job["max_runtime_ms"], 10, 3_600_000, "max runtime")
    _number(job["max_output_bytes"], 128, 65536, "max bounded output")
    _number(job["memory_limit_bytes"], 64 * 1024 * 1024,
            16 * 1024 * 1024 * 1024, "job memory")


def _validate(data):
    _assert_keys(data, _FIELDS)
    _number(data["format_version"], 1, 1, "execution profile version")
    for key in ("profile_id", "installation_id", "generation"):
        _hex(data[key], _HEX32, key)
    for key in ("policy_sha256", "publisher_key_sha256", "executable_sha256"):
        _hex(data[key], _HEX64, key)
    _sid(data["owner_sid"])
    for key in ("release_sequence", "policy_generation"):
        _number(data[key], 0, (1 << 63) - 1, key)
    _windows_local_path(data["executable"])
    _windows_local_path(data["cwd"])
    if not data["executable"].casefold().endswith(".exe"):
        raise ContractError("explicit executable image required")
    _path_roots(data)
    _environment(data)
    _token(data)
    _job(data)
    if type(data["network_mode"]) is not str or data["network_mode"] not in (
            "NO_NETWORK_ASSERTED", "NETWORK_POLICY_REVIEW_REQUIRED"):
        raise ContractError("explicit network mode required")
    _boolean(data["allow_path_search"], "PATH lookup", False)
    _boolean(data["inherit_parent_environment"], "ambient environment", False)


@dataclass(frozen=True, slots=True)
class FrozenExecutionProfile:
    canonical_bytes: bytes
    sha256: str

    def __post_init__(self):
        if type(self.canonical_bytes) is not bytes or type(self.sha256) is not str:
            raise ContractError("exact immutable execution profile bytes required")
        data = _decode(self.canonical_bytes, MAX_PROFILE_BYTES)
        _validate(data)
        expected = sha256(DOMAIN + self.canonical_bytes).hexdigest()
        if expected != self.sha256:
            raise ContractError("execution profile digest mismatch")


def freeze_execution_profile(data):
    """Frozen asserted recipe; no trust/Owner/Windows execution authority."""
    _validate(data)
    raw = _canonical(data, MAX_PROFILE_BYTES)
    return FrozenExecutionProfile(raw, sha256(DOMAIN + raw).hexdigest())


def parse_execution_profile(raw):
    data = _decode(raw, MAX_PROFILE_BYTES)
    _validate(data)
    return FrozenExecutionProfile(raw, sha256(DOMAIN + raw).hexdigest())


def bind_command_to_profile(command_data, profile):
    """Derive U2-b profile digest, checking the overlapping frozen fields.

    This is not an Owner GUI review: the caller can forge BOTH dictionaries.
    Only the protected Broker may later establish executable/Owner/provenance.
    """
    if type(command_data) is not dict or type(profile) is not FrozenExecutionProfile:
        raise ContractError("invalid command/profile types")
    # Recheck the digest at the handoff boundary: even a frozen Python
    # dataclass may be forged via object.__setattr__ in a caller process.
    FrozenExecutionProfile(profile.canonical_bytes, profile.sha256)
    p = _decode(profile.canonical_bytes, MAX_PROFILE_BYTES)
    _validate(p)
    from .offline_b01_full_binding import _validate_snapshot
    _validate_snapshot(command_data)
    for name in ("installation_id", "generation", "release_sequence",
                 "policy_generation", "owner_sid", "executable", "cwd"):
        if command_data[name] != p[name]:
            raise ContractError("command/profile " + name + " mismatch")
    if (command_data["timeout_ms"] > p["job"]["max_runtime_ms"]
            or command_data["output_limit"] > p["job"]["max_output_bytes"]):
        raise ContractError("command exceeds execution profile job budget")
    if command_data["execution_profile_sha256"] != profile.sha256:
        raise ContractError("unbound execution profile digest")
    return freeze_command(command_data)


def prepare_command_profile_binding(command_data, profile):
    """Produce a NEW command snapshot with profile digest installed.

    No service request, nonce, Owner approval, COMMIT receipt or process launch.
    """
    if type(command_data) is not dict or type(profile) is not FrozenExecutionProfile:
        raise ContractError("explicit command/profile required")
    candidate = {**command_data, "execution_profile_sha256": profile.sha256}
    return bind_command_to_profile(candidate, profile)
