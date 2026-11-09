"""Phase 9G: pure Windows code/data separation PLAN, not an ACL editor.

No filesystem reads/writes, elevation, installation, Task, or MCP listener.
Rendering a plan never establishes that any directory is protected.
"""
from dataclasses import dataclass, asdict
import json
import ntpath
from pathlib import Path
import re

# Casefolded roots a future installer must refuse on this machine. None by default:
# the names of a machine's other projects do not belong in published source.
PROTECTED_ROOTS = ()

class ReleaseLayoutError(ValueError):
    pass

@dataclass(frozen=True)
class ReleaseLayout:
    development_root: str
    code_root: str
    data_root: str

    def to_json(self):
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=True)

def _path(value):
    if type(value) is not str or len(value) < 4 or len(value) > 240:
        raise ReleaseLayoutError("a concrete absolute Windows path is required")
    if not re.fullmatch(r"[A-Za-z]:\\[^:*?\"<>|]+", value):
        raise ReleaseLayoutError("no UNC, device, relative or invalid path")
    if any(part in (".", "..", "") or part.endswith((" ", ".")) or
           ":" in part for part in value[3:].split("\\")):
        raise ReleaseLayoutError("ambiguous or traversal path")
    n = ntpath.normpath(value).casefold()
    if n.rstrip("\\") in ("c:", "d:", "e:"):
        raise ReleaseLayoutError("volume roots cannot be used")
    return n.rstrip("\\")

def _overlap(a, b):
    return a == b or a.startswith(b+"\\") or b.startswith(a+"\\")

def review_release_layout(*, development_root, code_root, data_root):
    """Explicit non-overlapping absolute Windows roots for a future installer.

    Only syntactic safety and separation, not authorization or OS ACL checks.
    """
    dev, code, data = (_path(p) for p in (development_root, code_root, data_root))
    if _overlap(dev, code) or _overlap(dev, data) or _overlap(code, data):
        raise ReleaseLayoutError("code, data and editable development roots overlap")
    if any(_overlap(p, r) for p in (code, data) for r in PROTECTED_ROOTS):
        raise ReleaseLayoutError("protected existing project/legacy root")
    return ReleaseLayout(development_root, code_root, data_root)

DEFAULT_LAYOUT = {
    "development_root": str(Path(__file__).resolve().parents[2]),
    # Proposed ONLY: a future admin-owned installation, not created by this code.
    "code_root": r"C:\Program Files\SoraCommanderNext",
    # Proposed ONLY: user-writable state/logs, never used to import trusted code.
    "data_root": r"C:\Users\Owner\AppData\Local\SoraCommanderNext",
}

REQUIRED_ACL_REVIEWS = (
    "Installer identity and elevation confirmed out of band",
    "Code-root DACL and every ancestor reviewed; no untrusted Modify/WriteDac/WriteOwner",
    "Code-root owner, inherited ACEs, reparse points and aliases inspected",
    "No same-Owner writable import path, dialog code, interpreter or DLL search path",
    "Data/log directory separately user writable; no code imported from it",
    "Publisher signature and pinned release manifest validated by trusted verifier",
    "Runtime behavior and all Owner GUI/lock/recovery tests repeated after install",
)

def layout_specification(layout):
    """Data-only actionable checklist; all unverified items remain HOLD."""
    if type(layout) is not ReleaseLayout:
        raise ReleaseLayoutError("reviewed release layout required")
    review_release_layout(**asdict(layout))
    return {
        "phase": "9G",
        "state": "DESIGN_ONLY",
        "approved_for_install": False,
        "approved_for_remote": False,
        "paths": asdict(layout),
        "required_checks": list(REQUIRED_ACL_REVIEWS),
        "never_modify": ["legacy Task", "RDC configuration", "existing deployments"],
    }
