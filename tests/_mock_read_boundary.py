"""Option B algorithm fixtures, NOT production path-policy acceptance.

Only explicitly registered synthetic paths and one exact root tuple are admitted.
All IO, metadata, byte limits, digests and identity rechecks remain real. Use only
inside inherited_test_temp(), never for secrets or production/Owner state.
"""
from contextlib import contextmanager, ExitStack
import importlib
import os
from pathlib import Path
from unittest.mock import patch

from sora_commander_next import file_reads as _reads
from sora_commander_next.file_reads import ReadBoundaryError

_FIXTURE_PARENT = Path(__file__).resolve().parents[1] / "temp"
# Modules that import _check by value; each alias must see the same admission.
_CHECK_ALIASES = ("file_edits", "dev_rdc_compat", "offline_output_spool",
                  "offline_codex_exact_file_evidence")


@contextmanager
def mock_read_boundary(root, names, *, preview=False):
    allowed = {str(root): root}
    for name in names:
        # Explicit portable relative names only; never resolve, glob, or admit
        # a subtree implicitly. Each nested directory/file needs registration.
        if (type(name) is not str or "\\" in name or ":" in name
                or "\0" in name or any(part in ("", ".", "..")
                                       for part in name.split("/"))):
            raise ValueError("register exact relative synthetic paths only")
        allowed[str(root / name)] = root / name

    def check(path, roots):
        if tuple(roots) != (str(root),) or path not in allowed:
            raise ReadBoundaryError("outside declared mock read fixture")
        return allowed[path]

    with ExitStack() as stack:
        boundary = stack.enter_context(patch(
            "sora_commander_next.file_reads._check", side_effect=check))
        if preview:
            # file_edits imports _check by value; its reader uses file_reads.
            stack.enter_context(patch(
                "sora_commander_next.file_edits._check", side_effect=check))
        yield boundary


def _below_fixture_parent(path):
    """Lexical parts below canonical temp/ for an scn-synthetic child, else None."""
    parent = tuple(part.casefold() for part in _FIXTURE_PARENT.parts)
    parts = Path(path).parts
    if (len(parts) <= len(parent)
            or tuple(part.casefold() for part in parts[:len(parent)]) != parent
            or not parts[len(parent)].startswith("scn-synthetic-")):
        return None
    return parts[len(parent):]


@contextmanager
def canonical_fixture_admission():
    """Real _check below inherited_test_temp(); only the temp/ ancestor is exempt.

    NOT production path-policy acceptance. Every component below canonical temp/
    is still checked against the full production name policy, and any other path
    with a "temp" component is still rejected before the real check runs. Real
    resolution, root containment, link and later IO/identity checks are unchanged.
    Module-level patches assume sequential tests in one interpreter.
    """
    real_check = _reads._check
    full_components = _reads._READ_DENY_COMPONENTS
    real_name_denied = _reads._read_name_denied

    def denied(part):
        return part.casefold() in full_components or real_name_denied(part)

    def check(path, roots):
        try:
            spelled = os.fspath(path)
        except TypeError:
            spelled = None
        if type(spelled) is not str:
            return real_check(path, roots)  # Unchanged rejection of odd input.
        below = _below_fixture_parent(spelled)
        if below is not None:
            if any(denied(part) for part in below):
                raise ReadBoundaryError("sensitive path excluded from read scope")
        elif any(part.casefold() == "temp" for part in Path(spelled).parts[1:]):
            raise ReadBoundaryError("sensitive path excluded from read scope")
        return real_check(path, roots)

    with ExitStack() as stack:
        stack.enter_context(patch.object(
            _reads, "_READ_DENY_COMPONENTS", full_components - {"temp"}))
        stack.enter_context(patch.object(_reads, "_check", check))
        for name in _CHECK_ALIASES:
            module = importlib.import_module("sora_commander_next." + name)
            if getattr(module, "_check", None) is real_check:
                stack.enter_context(patch.object(module, "_check", check))
        yield check
