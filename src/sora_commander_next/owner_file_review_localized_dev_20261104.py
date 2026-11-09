"""DEV_ONLY / NOT_REGISTERED: localized file review text. No file_edits path uses it.

Display data only: no Tk at import, no decision, deadline, write or authority.
Every call checks that the preview and the caller's expected target agree, then
puts path, SHA-256 values, review ID, expiry and the complete proposal on screen.
Saving keeps its own checks in file_edits.py; this module cannot replace them.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import re

from .file_edits import escape_review_display
from .owner_review_i18n import fill, load_catalog
from .owner_review_presentation_candidate import ReviewPresentationCandidate
from .owner_review_renderer import ReviewScreen

DEV_ONLY = True
NOT_REGISTERED = True
_SHA = re.compile("[0-9a-f]{64}")
# Kept here because the catalogs are pinned by SHA-256 and the running command window reads them.
_EXTRA_WORDS = {
    "en": {"heading_file_directory": "Confirm local folder creation",
           "file_operation_directory": "Create a new folder",
           "file_full_text": "=== Complete proposed content ==="},
    "ja": {"heading_file_directory": "ローカルフォルダ作成の確認",
           "file_operation_directory": "新しいフォルダの作成",
           "file_full_text": "=== 提案内容の全文 ==="},
}


@dataclass(frozen=True, slots=True)
class BoundFileReview:
    operation: str
    binding_sha256: str  # for comparing displays; never authentication or approval
    presentation: ReviewPresentationCandidate


def _text(value, label):
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError("invalid " + label + " for file review")
    return value


def _sha(value, label):
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError("invalid " + label + " for file review")
    return value


def _review_time(review_id, seconds):
    _text(review_id, "review ID")
    if type(seconds) not in (int, float) or not 0 < seconds <= 60:
        raise ValueError("invalid expiry for file review")


def _build(operation, kind, heading_key, facts, proposal_title, proposal, *,
           review_id, seconds, locale):
    words = {**load_catalog(locale), **_EXTRA_WORDS[locale]}
    lines = [fill(words["file_operation"], words["file_operation_" + operation])]
    lines += [fill(words[key], value) for key, value in facts]
    lines += [fill(words["body_review_id"], repr(review_id)),
              fill(words["body_expires"], f"{seconds:.0f}")]
    original = "\n".join(lines) + "\n"
    if proposal_title is not None:
        original += "\n" + words[proposal_title] + "\n" + proposal
    screen = ReviewScreen(
        kind=kind, title=words["window_title"], heading=words[heading_key],
        caution=words["file_caution"], information=words["file_information"], body=original)
    labels = (words["reject"], words["approve_once"], words["repeat_disabled"])
    seal = json.dumps([operation, list(facts), review_id, seconds, proposal],
                      ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return BoundFileReview(operation, sha256(seal).hexdigest(), ReviewPresentationCandidate(
        screen=screen, labels=labels, locale=locale, original_body=original, caution_level=None))


def bound_create(preview, *, expected_path, expected_new_sha256, review_id, seconds, locale):
    """One preview_text_create result, refused unless it matches the expected target."""
    if type(preview) is not dict or preview.get("review_truncated") is not False:
        raise ValueError("incomplete file creation preview")
    _review_time(review_id, seconds)
    path = _text(preview.get("path"), "path")
    new_sha = _sha(preview.get("new_sha256"), "new SHA-256")
    size = preview.get("bytes")
    if path != _text(expected_path, "path") or new_sha != _sha(expected_new_sha256, "new SHA-256"):
        raise ValueError("file creation preview does not match the expected target")
    if type(size) is not int or size < 0:
        raise ValueError("invalid file size for file review")
    review = _text(preview.get("review"), "proposal")
    facts = (("file_path", escape_review_display(path)), ("file_size", str(size)),
             ("file_new_sha", new_sha))
    return _build("create", "file_create", "heading_file_create", facts, "file_full_content",
                  review, review_id=review_id, seconds=seconds, locale=locale)


def bound_edit(preview, *, expected_path, expected_old_sha256, expected_new_sha256,
               review_id, seconds, locale):
    """One preview_text_edit result, with the complete diff and both SHA-256 values."""
    if type(preview) is not dict or preview.get("diff_truncated") is not False:
        raise ValueError("incomplete file edit preview")
    if preview.get("changed") is not True:
        raise ValueError("file edit preview changes nothing")
    _review_time(review_id, seconds)
    path = _text(preview.get("path"), "path")
    old_sha = _sha(preview.get("old_sha256"), "current SHA-256")
    new_sha = _sha(preview.get("new_sha256"), "new SHA-256")
    if (path != _text(expected_path, "path")
            or old_sha != _sha(expected_old_sha256, "current SHA-256")
            or new_sha != _sha(expected_new_sha256, "new SHA-256")):
        raise ValueError("file edit preview does not match the expected target")
    diff = escape_review_display(_text(preview.get("diff"), "diff"))
    facts = (("file_path", escape_review_display(path)), ("file_old_sha", old_sha),
             ("file_new_sha", new_sha))
    return _build("edit", "file_edit", "heading_file_edit", facts, "file_full_diff", diff,
                  review_id=review_id, seconds=seconds, locale=locale)


def bound_move(proposal, *, expected_source, expected_destination, review_id, seconds, locale):
    """One move proposal from dev_rdc_compat; a move carries no content to show."""
    if type(proposal) is not dict or proposal.keys() != {"operation", "source", "destination"}:
        raise ValueError("unexpected file move proposal")
    if proposal["operation"] != "move_file":
        raise ValueError("unexpected file move proposal")
    _review_time(review_id, seconds)
    source = _text(proposal["source"], "source")
    destination = _text(proposal["destination"], "destination")
    if (source != _text(expected_source, "source")
            or destination != _text(expected_destination, "destination")
            or source == destination):
        raise ValueError("file move proposal does not match the expected target")
    facts = (("file_source", escape_review_display(source)),
             ("file_destination", escape_review_display(destination)))
    return _build("move", "file_edit", "heading_file_move", facts, None, "",
                  review_id=review_id, seconds=seconds, locale=locale)


def _proposal(value, operation, keys):
    if type(value) is not dict or value.keys() != keys or value["operation"] != operation:
        raise ValueError("unexpected file proposal")


def bound_source_patch(proposal, *, review_id, seconds, locale):
    """One dev_rdc_compat patch: the complete diff and both SHA-256 values.

    The whole file is not displayed. Its SHA-256 must match here, and the writer
    checks the source again before replacing it.
    """
    _proposal(proposal, "request_source_patch_dev", {"operation", "path", "old_sha256", "new_sha256",
                                                     "diff", "full_proposed_content"})
    content = proposal["full_proposed_content"]
    if type(content) is not str or sha256(content.encode("utf-8")).hexdigest() != proposal["new_sha256"]:
        raise ValueError("file patch proposal does not match its SHA-256")
    preview = {"path": proposal["path"], "old_sha256": proposal["old_sha256"], "new_sha256": proposal["new_sha256"],
               "diff": proposal["diff"], "diff_truncated": False, "changed": proposal["old_sha256"] != proposal["new_sha256"]}
    return bound_edit(preview, expected_path=proposal["path"], expected_old_sha256=proposal["old_sha256"],
                      expected_new_sha256=proposal["new_sha256"], review_id=review_id, seconds=seconds, locale=locale)


def bound_new_file(proposal, *, review_id, seconds, locale):
    """One dev_rdc_compat file creation, with its complete content."""
    _proposal(proposal, "create_file", {"operation", "path", "bytes", "sha256", "full_proposed_content"})
    _review_time(review_id, seconds)
    content = proposal["full_proposed_content"]
    if type(content) is not str:
        raise ValueError("invalid proposal for file review")
    data = content.encode("utf-8")
    size, new_sha = proposal["bytes"], _sha(proposal["sha256"], "new SHA-256")
    if type(size) is not int or size != len(data) or new_sha != sha256(data).hexdigest():
        raise ValueError("file creation proposal does not match its content")
    facts = (("file_path", escape_review_display(_text(proposal["path"], "path"))),
             ("file_size", str(size)), ("file_new_sha", new_sha))
    return _build("create", "file_create", "heading_file_create", facts, "file_full_text",
                  escape_review_display(content), review_id=review_id, seconds=seconds, locale=locale)


def bound_directory(proposal, *, review_id, seconds, locale):
    """One dev_rdc_compat folder creation; a folder carries no content to show."""
    _proposal(proposal, "create_directory", {"operation", "path"})
    _review_time(review_id, seconds)
    facts = (("file_path", escape_review_display(_text(proposal["path"], "path"))),)
    return _build("directory", "file_create", "heading_file_directory", facts, None, "",
                  review_id=review_id, seconds=seconds, locale=locale)


def bound_proposal(proposal, *, review_id, seconds, locale):
    """Display data for one dev_rdc_compat proposal. Anything else is refused."""
    operation = proposal.get("operation") if type(proposal) is dict else None
    options = dict(review_id=review_id, seconds=seconds, locale=locale)
    if operation == "request_source_patch_dev":
        return bound_source_patch(proposal, **options)
    if operation == "create_file":
        return bound_new_file(proposal, **options)
    if operation == "create_directory":
        return bound_directory(proposal, **options)
    if operation == "move_file":
        return bound_move(proposal, expected_source=proposal.get("source"),
                          expected_destination=proposal.get("destination"), **options)
    raise ValueError("unexpected file proposal")
