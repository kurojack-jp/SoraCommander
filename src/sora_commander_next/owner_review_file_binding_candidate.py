"""DEV-only file preview -> bilingual Owner presentation candidate.

Never used by live Owner controllers. The same copied preview supplies
both exact review text and summary facts; no approval, file IO or GUI.
"""
from dataclasses import dataclass
from hashlib import sha256
import json

from .owner_review_file_view import create_file_view, edit_file_view
from .owner_review_presentation_candidate import build_presentation_candidate


@dataclass(frozen=True, slots=True)
class BoundFilePresentation:
    kind: str
    preview_sha256: str
    presentation: object


def _copy_preview(preview):
    if type(preview) is not dict or any(type(k) is not str for k in preview):
        raise ValueError("invalid file preview")
    if any(type(v) not in (str, int, bool) for v in preview.values()):
        raise ValueError("non-scalar file preview field")
    return dict(preview)


def _seal(preview):
    encoded = json.dumps(preview, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("ascii")
    return sha256(encoded).hexdigest()


def bind_file_create_preview(preview, *, locale):
    snap = _copy_preview(preview)
    if snap.keys() != {"path", "bytes", "new_sha256",
                       "review", "review_truncated"}:
        raise ValueError("unexpected create preview shape")
    original = create_file_view(snap, snap["new_sha256"]).body
    facts = {"path": snap["path"], "bytes": snap["bytes"],
             "sha256": snap["new_sha256"]}
    view = build_presentation_candidate(
        locale=locale, kind="file_create", original_body=original,
        facts=facts)
    return BoundFilePresentation("file_create", _seal(snap), view)


def bind_file_edit_preview(preview, *, locale, complete_review=None):
    snap = _copy_preview(preview)
    if snap.keys() != {"path", "diff", "diff_truncated"}:
        raise ValueError("unexpected edit preview shape")
    if complete_review is not None and type(complete_review) is not str:
        raise ValueError("invalid complete review")
    original = edit_file_view(snap, complete_review=complete_review).body
    facts = {"path": snap["path"]}
    view = build_presentation_candidate(
        locale=locale, kind="file_edit", original_body=original,
        facts=facts)
    # Include supplied complete review in diagnostic correlation only.
    digest = sha256((_seal(snap) + "\x00" +
                     (complete_review or "")).encode("utf-8")).hexdigest()
    return BoundFilePresentation("file_edit", digest, view)
