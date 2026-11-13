"""Presentation-only file review text for the shared Owner dialog (DEV candidate).

The current file_edits.py approval paths are NOT rerouted through this module.
This module owns no Tk, OS identity, callbacks, deadlines, decisions or writes.
The source SHA and complete proposal are displayed, never used as permission.
"""
from dataclasses import dataclass
import re
from .file_edits import escape_review_display

# Same text as the active pilot window (i18n key pilot_file_caution): Owner
# decision 2026-11-12, closing the window is "no answer", not a reject.
_CAUTION = ("Inspect the ENTIRE proposal. Reject declines. "
            "Closing the window or not answering in time ends the review without a decision; no file is changed.")
_INFORMATION = ("Close and unanswered expiry are not Owner rejection. "
                "No file is changed; the result is reported as no decision (TIMED_OUT).")


def _nonempty_text(value, label):
    if type(value) is not str or not value:
        raise ValueError(f"invalid {label} for Owner review")
    return value


@dataclass(frozen=True, slots=True)
class LocalFileReviewView:
    """Exact visual input for the existing single presentation renderer."""
    kind: str
    title: str
    heading: str
    body: str

    def __post_init__(self):
        # Files can never present an active command or a reusable permission.
        if type(self.kind) is not str or self.kind not in ("file_create", "file_edit"):
            raise ValueError("unknown local file Owner review kind")
        for field in ("title", "heading", "body"):
            _nonempty_text(getattr(self, field), field)

    def render_kwargs(self):
        # Return fresh values: the caller still owns its distinct click mapping.
        return {
            "kind": self.kind, "title": self.title, "heading": self.heading,
            "caution": _CAUTION, "information": _INFORMATION,
            "body": self.body,
        }


def create_file_view(preview, expected_sha256):
    """Match current file CREATE review text, with no truncation or authorization."""
    if type(preview) is not dict or preview.get("review_truncated") is not False:
        raise ValueError("incomplete file creation preview")
    path = _nonempty_text(preview.get("path"), "file path")
    review = _nonempty_text(preview.get("review"), "file content")
    size = preview.get("bytes")
    if type(size) is not int or size < 0:
        raise ValueError("invalid file length")
    if type(expected_sha256) is not str or re.fullmatch("[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("invalid displayed SHA")
    if preview.get("new_sha256") != expected_sha256:
        raise ValueError("creation preview hash changed")
    return LocalFileReviewView(
        kind="file_create",
        title="Sora Commander Next - LOCAL file creation review",
        heading="LOCAL file create — Review complete JSON-escaped UTF-8 text.",
        body=f"{escape_review_display(path)}\n{size} bytes\nSHA-256: {expected_sha256}\n\n{review}",
    )


def edit_file_view(preview, *, complete_review=None,
                   title="LOCAL file edit review",
                   wording="One-time local file edit. Inspect the complete diff."):
    """Match current file EDIT review text exactly, including supplied full diff."""
    if type(preview) is not dict:
        raise ValueError("invalid edit preview")
    path = _nonempty_text(preview.get("path"), "file path")
    diff = _nonempty_text(preview.get("diff"), "edit diff")
    if type(preview.get("diff_truncated")) is not bool:
        raise ValueError("missing explicit edit diff completeness")
    if preview["diff_truncated"] and complete_review is None:
        raise ValueError("truncated edit diff without full review")
    if complete_review is not None:
        _nonempty_text(complete_review, "complete review")
    title = _nonempty_text(title, "review title")
    wording = _nonempty_text(wording, "review wording")
    return LocalFileReviewView(
        kind="file_edit",
        title="Sora Commander Next - " + title,
        heading=title + " — " + wording,
        body=escape_review_display(path) + "\n\n" + escape_review_display(
            diff if complete_review is None else complete_review),
    )
