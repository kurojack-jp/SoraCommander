"""DEV_ONLY exact-file Codex evidence reader, not acceptance or authority.

Reads ONLY caller-selected, validated repo-relative file paths; never enumerates
the repository, reads job secrets, executes commands or writes any files.
Designed to supply untrusted observations to offline_codex_task_acceptance.
An exact read is not Windows strong CAS and cannot attest Codex authorship.
"""
from pathlib import Path
from .file_reads import _check, _read_inspection_text, ReadBoundaryError
from .offline_codex_task_acceptance import (
    FileEvidence, RequiredFile, TaskReceiptError, MAX_ITEMS,
)

MAX_EVIDENCE_BYTES = 65536


class ExactEvidenceError(ValueError):
    """Explicit evidence unavailable; never silently omit or substitute it."""


def observe_exact_files(repo_root, required_files):
    """One bounded, specified file read per target. No recursive traversal.

    Missing files produce explicit None observations only if the parent path
    is verified inside the supplied root. Unreadable/replaced/linked files
    abort the entire batch; an empty result must not disguise access refusal.
    No filesystem audit of *other* changed files is implied.
    """
    if (type(repo_root) is not str or not repo_root
            or type(required_files) is not tuple
            or not 1 <= len(required_files) <= MAX_ITEMS
            or any(type(r) is not RequiredFile for r in required_files)):
        raise ExactEvidenceError("trusted root and exact required file tuple needed")
    p = Path(repo_root)
    if not p.is_absolute():
        raise ExactEvidenceError("absolute explicit repo root required")
    unique = [r.path.casefold() for r in required_files]
    if len(set(unique)) != len(unique):
        raise ExactEvidenceError("duplicate Windows file targets")
    # Reuse the existing linked-root boundary; no Path.rglob(), git status, or
    # fallback when a file cannot be safely inspected.
    try:
        checked_root = _check(repo_root, (repo_root,))
        if not checked_root.is_dir():
            raise ExactEvidenceError("repository root unavailable")
        out = []
        for required in required_files:
            target = checked_root.joinpath(*required.path.split("/"))
            # Resolve/verify parent first so missing paths are never accepted
            # when the containing directory or an ancestor is inaccessible.
            _check(str(target.parent), (str(checked_root),))
            if target.is_symlink() or target.is_junction():
                raise ExactEvidenceError("linked target rejected")
            try:
                target.lstat()
            except FileNotFoundError:
                # Absent BEFORE inspection: don't confuse a file that
                # disappears DURING a read with a provably absent candidate.
                _check(str(target.parent), (str(checked_root),))
                if target.exists() or target.is_symlink() or target.is_junction():
                    raise ExactEvidenceError("target changed while missing")
                digest = None
            else:
                # If this path disappears or changes during inspection,
                # the existing reader raises. Never convert that to absence.
                digest = _read_inspection_text(
                    str(target), (str(checked_root),), MAX_EVIDENCE_BYTES
                )["sha256"]
            out.append(FileEvidence(required.path, digest))
        return tuple(out)
    except (OSError, ReadBoundaryError, UnicodeError, TaskReceiptError) as exc:
        raise ExactEvidenceError("exact file evidence unavailable") from exc
