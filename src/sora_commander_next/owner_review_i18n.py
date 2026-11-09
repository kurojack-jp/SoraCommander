"""Strict bilingual Owner review presentation prototype (NOT active in Host).

Only fixed, SHA-pinned bundled JSON dictionaries supply UI wording.
The exact original review payload is preserved byte-for-byte as a str.
Summaries are deterministic, supplementary, never authority or approval.
No GUI, callbacks, timers, OS/session checks, private file writes or launch.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType

# Add or change a locale only with code review and an explicit new digest.
_LOCALE_FILES = MappingProxyType({
    "en": ("en.json", "e1cf67b8d8115eca128be0fa18695ef4f42d59dfb422da1d3d4599aea7d4c72f"),
    "ja": ("ja.json", "95ee3765273a8f43ee029792be2259f726ae1fceb671837b6f63880f3319d935"),
})
_SUPPORTED_KEYS = frozenset({
    "window_title", "heading_command", "heading_file_create",
    "heading_file_edit", "heading_v2_candidate", "summary_title",
    "full_details_title", "summary_warning", "reject", "approve_once",
    "repeat_disabled", "approve_trusted_candidate", "caution",
    "information", "command_close_expiry_notice", "action", "action_command", "action_file_create",
    "action_file_edit", "action_v2_candidate", "target",
    "working_directory", "argument_count", "file_bytes", "sha256",
    "risk", "risk_command", "risk_file_create", "risk_file_edit",
    "risk_v2_candidate", "trusted_option", "yes", "no", "unavailable",
    "body_executable", "body_argv", "body_cwd", "body_timeout",
    "body_output_limit", "body_review_id", "body_expires",
    "body_unknown_effects", "body_not_remote",
    "heading_file_move", "file_caution", "file_information", "file_operation",
    "file_operation_create", "file_operation_edit", "file_operation_move",
    "file_path", "file_source", "file_destination", "file_size",
    "file_old_sha", "file_new_sha", "file_full_content", "file_full_diff",
})
_KINDS = frozenset({"command", "file_create", "file_edit", "v2_candidate"})
_FACT_FIELDS = MappingProxyType({
    "command": frozenset({"executable", "cwd", "argv"}),
    "file_create": frozenset({"path", "bytes", "sha256"}),
    "file_edit": frozenset({"path"}),
    "v2_candidate": frozenset({"executable", "cwd", "argv", "allow_trusted"}),
})


def _exact_str(value, name):
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError("invalid " + name)
    return value


def load_catalog(locale="en"):
    """English is mandatory; only an absent Japanese file falls back.

    Changed, partial, unreadable and malformed dictionaries remain errors.
    No unverified per-key merge is possible with the pinned catalog contract.
    """
    if type(locale) is not str or locale not in _LOCALE_FILES:
        raise ValueError("unsupported Owner UI locale")
    english = _load_catalog_exact("en")
    if locale == "en":
        return english
    try:
        return _load_catalog_exact("ja")
    except FileNotFoundError:
        return english


def _load_catalog_exact(locale):
    """Return only a SHA-verified, copy-safe fixed package translation map.

    A local preference is NOT read here and the caller cannot specify a path.
    Reject missing, changed, malformed, wrong-key or excessive dictionaries.
    """
    if type(locale) is not str or locale not in _LOCALE_FILES:
        raise ValueError("unsupported Owner UI locale")
    filename, expected_digest = _LOCALE_FILES[locale]
    directory = Path(__file__).resolve().parent / "i18n"
    file_path = directory / filename
    if directory.is_symlink() or file_path.is_symlink():
        raise ValueError("linked Owner UI locale resource")
    with file_path.open("rb") as source:
        data = source.read(8193)
    if len(data) > 8192 or hashlib.sha256(data).hexdigest() != expected_digest:
        raise ValueError("Owner UI locale resource integrity failure")
    try:
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate Owner translation key")
                result[key] = value
            return result
        words = json.loads(data.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("malformed Owner UI locale resource") from exc
    if type(words) is not dict or words.keys() != _SUPPORTED_KEYS:
        raise ValueError("unexpected Owner UI translation keys")
    for value in words.values():
        if (type(value) is not str or not 1 <= len(value) <= 350
                or any(ord(c) < 32 for c in value)):
            raise ValueError("unsafe Owner UI translation text")
    return dict(words)


def available_locales():
    return ("en", "ja")


def fill(template, value):
    """Put one display value into one catalog label. The value is never parsed."""
    if type(template) is not str or template.count("{value}") != 1 or type(value) is not str:
        raise ValueError("invalid Owner review label template")
    return template.replace("{value}", value)


def _field(value, name):
    # Escaped and bounded for SUMMARY ONLY; complete source remains below.
    value = _exact_str(value, name)
    escaped = json.dumps(value, ensure_ascii=True)
    return escaped if len(escaped) <= 250 else escaped[:247] + "..."


@dataclass(frozen=True, slots=True)
class LocalizedReviewDraft:
    """Display-only proposal. render_kwargs does not carry security decisions."""
    locale: str
    kind: str
    title: str
    heading: str
    caution: str
    information: str
    body: str
    original_body: str
    summary: str
    buttons: tuple

    def render_kwargs(self):
        # Fits the existing shared renderer's current read-only presentation.
        return {"kind": self.kind, "title": self.title,
                "heading": self.heading, "caution": self.caution,
                "information": self.information, "body": self.body}


def prepare_localized_review(*, locale, kind, original_body, facts):
    """Build a bilingual candidate using exact original evidence and fixed fields.

    Only code-chosen kind and fields affect the summary. This function does
    not verify that the facts correspond to original_body: the independent
    Owner controller MUST derive both from its same validated request.
    """
    words = load_catalog(locale)
    if type(kind) is not str or kind not in _KINDS:
        raise ValueError("unknown Owner review kind")
    # Exact review evidence may legally contain NUL; never normalize it away.
    if type(original_body) is not str or not original_body:
        raise ValueError("invalid original review text")
    if type(facts) is not dict or facts.keys() != _FACT_FIELDS[kind]:
        raise ValueError("missing or extra Owner summary fields")
    action = words["action_" + kind]
    risk = words["risk_" + kind]
    lines = [words["summary_title"],
             words["action"] + ": " + action]
    if kind in ("command", "v2_candidate"):
        lines += [
            words["target"] + ": " + _field(facts["executable"], "executable"),
            words["working_directory"] + ": " + _field(facts["cwd"], "cwd"),
        ]
        argv = facts["argv"]
        if type(argv) is not list or len(argv) > 128 or any(type(v) is not str for v in argv):
            raise ValueError("invalid command summary argv")
        lines.append(words["argument_count"] + ": " + str(len(argv)))
        if kind == "v2_candidate":
            if type(facts["allow_trusted"]) is not bool:
                raise ValueError("invalid v2 summary trusted switch")
            lines.append(words["trusted_option"] + ": " +
                         words["yes" if facts["allow_trusted"] else "no"])
    else:
        lines.append(words["target"] + ": " + _field(facts["path"], "file path"))
        if kind == "file_create":
            size = facts["bytes"]
            digest = facts["sha256"]
            if type(size) is not int or not 0 <= size <= 65536:
                raise ValueError("invalid proposed file size")
            if type(digest) is not str or re.fullmatch("[0-9a-f]{64}", digest) is None:
                raise ValueError("invalid proposed SHA-256")
            lines.append(words["file_bytes"] + ": " + str(size))
            lines.append(words["sha256"] + ": " + digest)
    lines.append(words["risk"] + ": " + risk)
    summary = "\n".join(lines)
    # The original body is embedded EXACTLY and in full after a visible divider.
    display_body = ("[" + words["summary_warning"] + "]\n" + summary
                    + "\n\n=== " + words["full_details_title"] + " ===\n"
                    + original_body)
    buttons = (words["reject"], words["approve_once"],
               words["approve_trusted_candidate"] if kind == "v2_candidate"
               else words["repeat_disabled"])
    return LocalizedReviewDraft(
        locale=locale, kind=kind, title=words["window_title"],
        heading=words["heading_" + kind], caution=words["caution"],
        information=words["information"], body=display_body,
        original_body=original_body, summary=summary, buttons=buttons,
    )
