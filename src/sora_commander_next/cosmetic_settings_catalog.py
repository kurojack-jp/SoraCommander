"""Pure, non-mutating allowlist prototype for Chat-driven decorative settings.

NOT an MCP tool, not an Owner approval workflow, not an image decoder, and
not a preference store.  No filesystem, UI, network or import-time effects.
Only explicitly catalogued, non-security cosmetic names can be mentioned.
"""
from dataclasses import dataclass
import re
from types import MappingProxyType


class SettingsRequestError(ValueError):
    """Unrecognized/malformed request; do not infer an alternate setting."""


@dataclass(frozen=True)
class SettingSpec:
    key: str
    kind: str
    default: str
    availability: str


# The code, NOT a JSON file or chat-supplied dictionary, determines the keys.
# Adding another key requires code review plus separate Owner approval.
_PORTRAIT = SettingSpec(
    key="ui.owner_dialog.portrait",
    kind="image/png",
    default="bundled_sora_256",
    availability="DESIGN_ONLY",
)
_ALLOWLIST = MappingProxyType({_PORTRAIT.key: _PORTRAIT})
_SCHEMA_VERSION = 1
_ABSENT = object()
_DEVICES = frozenset({"CON", "PRN", "AUX", "NUL"} |
                     {f"COM{i}" for i in range(1, 10)} |
                     {f"LPT{i}" for i in range(1, 10)})


def _local_png_path_syntax(value) -> bool:
    """Syntactic rejection only; NEVER proves path/root, content or ownership."""
    if type(value) is not str or not 7 <= len(value) <= 1024:
        return False
    if not re.fullmatch(r"[A-Za-z]:\\[^\\/\x00-\x1f]+(?:\\[^\\/\x00-\x1f]+)*", value):
        return False
    parts = value[3:].split("\\")
    for part in parts:
        if (part in (".", "..") or part[-1] in (".", " ")
                or any(char in part for char in '<>:"|?*')
                or part.split(".", 1)[0].upper() in _DEVICES):
            return False
    return parts[-1].lower().endswith(".png")


def _catalogue_entry(spec: SettingSpec) -> dict:
    # New mutable objects on every call. No local/private paths leak.
    return {
        "key": spec.key,
        "type": spec.kind,
        "default": spec.default,
        "effective": None,  # no user preference store has been observed
        "effective_verified": False,
        "availability": spec.availability,
        "writable": False,
    }


def list_settings() -> dict:
    """Read-only public design catalog; never claims a registered write tool."""
    return {
        "status": "OK",
        "schema_version": _SCHEMA_VERSION,
        "registered_mcp_tool": False,
        "settings": [_catalogue_entry(spec) for spec in _ALLOWLIST.values()],
    }


def get_setting(key: str) -> dict:
    """Return the known bundled default, not a yet-nonexistent user preference."""
    if type(key) is not str or key not in _ALLOWLIST:
        raise SettingsRequestError("unknown or invalid setting key")
    result = _catalogue_entry(_ALLOWLIST[key])
    return {"status": "OK", "schema_version": _SCHEMA_VERSION,
            "registered_mcp_tool": False, "setting": result}


def evaluate_settings_request(*, action, key=_ABSENT, value=_ABSENT,
                              preview_id=_ABSENT) -> dict:
    """Fail-closed shape validator for the *future* public action contract.

    No preview/apply/reset side effects are implemented here.  A preview is
    NOT a reservation or authorization, so no tokens are minted.
    """
    if type(action) is not str:
        raise SettingsRequestError("action must be an exact string")
    if action == "list":
        if any(v is not _ABSENT for v in (key, value, preview_id)):
            raise SettingsRequestError("list accepts no other fields")
        return list_settings()
    if action == "get":
        if value is not _ABSENT or preview_id is not _ABSENT:
            raise SettingsRequestError("unexpected get arguments")
        return get_setting(key)
    if action not in ("preview", "apply", "reset"):
        raise SettingsRequestError("unknown action")
    if type(key) is not str or key not in _ALLOWLIST:
        raise SettingsRequestError("unknown or invalid setting key")
    if action == "preview":
        if not _local_png_path_syntax(value) or preview_id is not _ABSENT:
            raise SettingsRequestError("invalid preview arguments")
    elif action == "apply":
        if value is not _ABSENT or type(preview_id) is not str or not preview_id:
            raise SettingsRequestError("invalid apply arguments")
    else:
        if value is not _ABSENT or preview_id is not _ABSENT:
            raise SettingsRequestError("reset accepts key only")
    return {
        "status": "NOT_ENABLED",
        "reason": "Owner-approved storage and mutation contract not yet accepted",
        "key": key,
        "action": action,
        "changed": False,
        "approval_granted": False,
        "source_validated": False,  # syntax is NOT file/root/decoder validation
    }
