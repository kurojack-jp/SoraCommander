"""DEV-only integrated bilingual Owner presentation candidate.

No production command, file-edit or v2 adapter calls this module yet.
Preserves all original review text and delegates to the existing single
renderer. Translates ONLY three button captions, never their callbacks,
enabled state or decision codes. The controller must bind facts and body
to its SAME previously validated immutable request.
"""
from dataclasses import dataclass
import unicodedata

from .owner_review_i18n import prepare_localized_review
from .owner_command_caution import command_caution
from .owner_review_renderer import ReviewScreen, render_review


@dataclass(frozen=True, slots=True)
class ReviewPresentationCandidate:
    screen: ReviewScreen
    labels: tuple
    locale: str
    original_body: str
    caution_level: str | None

    def render(self, tk, root, theme, *, reject, approve, trusted=None):
        """Optional paint-only candidate; actual Owner controller owns events."""
        captions = _CaptionOnlyTheme(theme, self.screen.kind, self.labels)
        return render_review(
            tk, root, captions, self.screen,
            reject=reject, approve=approve, trusted=trusted,
        )


class _CaptionOnlyTheme:
    """Transparent theme adapter changing text labels only.

    All other keyword arguments (especially command, disabled, primary)
    pass through without mutation. Unexpected renderer calls fail closed.
    """
    __slots__ = ("_delegate", "_labels", "_kind")

    def __init__(self, delegate, kind, labels):
        if type(kind) is not str or kind not in (
                "command", "file_create", "file_edit", "v2_candidate"):
            raise ValueError("unsupported review caption kind")
        if (type(labels) is not tuple or len(labels) != 3
                or any(type(label) is not str or not label
                       for label in labels)):
            raise ValueError("incomplete Owner review captions")
        self._delegate = delegate
        self._labels = labels
        self._kind = kind

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def neon_button(self, tk, parent, *, column, text, **kwargs):
        expected = {
            0: "Reject",
            1: "Approve once" if self._kind == "v2_candidate" else "Approve ONCE",
            2: ("Approve this command until revoked"
                if self._kind == "v2_candidate"
                else "10-minute repeat permission (not enabled yet)"),
        }
        if type(column) is not int or column not in expected or text != expected[column]:
            raise ValueError("unexpected Owner button mapping")
        # Exactly the same callback, disabled and primary kwargs.
        return self._delegate.neon_button(
            tk, parent, column=column, text=self._labels[column], **kwargs)

def _check_review_visibility(body):
    """Stop if exact original text contains hard-to-distinguish Unicode controls.

    Do not normalize, strip or translate original evidence: a future controller
    can introduce an independently reviewed escaped-view mode if needed.
    """
    if type(body) is not str or not body:
        raise ValueError("complete original review body required")
    for char in body:
        if char not in ("\n", "\r", "\t") and unicodedata.category(char) in (
                "Cc", "Cf", "Zl", "Zp"):
            raise ValueError("Owner review body contains visually ambiguous controls")


def _copy_facts(kind, facts):
    """Bound a single display snapshot; this is NOT controller validation."""
    if type(facts) is not dict:
        raise ValueError("complete validated review facts required")
    snapshot = dict(facts)
    if kind in ("command", "v2_candidate"):
        argv = snapshot.get("argv")
        if type(argv) is not list:
            raise ValueError("unsupported command arguments")
        snapshot["argv"] = list(argv)
    return snapshot


def build_presentation_candidate(*, locale, kind, original_body, facts):
    """Prepare unified visual proof. Never grants or checks authorization."""
    _check_review_visibility(original_body)
    snapshot = _copy_facts(kind, facts)
    # A future controller MUST derive original_body and snapshot from the
    # SAME validated immutable request. This data-only API cannot prove it.
    localized = prepare_localized_review(
        locale=locale, kind=kind, original_body=original_body, facts=snapshot)
    caution_level = None
    body = localized.body
    if kind in ("command", "v2_candidate"):
        advice = command_caution(
            executable=snapshot["executable"], argv=snapshot["argv"],
            locale=locale)
        caution_level = advice.level
        # Advisory section is independent and cannot replace authoritative body.
        body = "\n".join(advice.display_lines()) + "\n\n" + body

    screen = ReviewScreen(
        kind=kind, title=localized.title, heading=localized.heading,
        caution=localized.caution, information=localized.information,
        body=body,
        trusted_offered=snapshot["allow_trusted"]
        if kind == "v2_candidate" else False,
    )
    if not screen.body.endswith(original_body):
        raise ValueError("original Owner review body lost")
    return ReviewPresentationCandidate(
        screen=screen, labels=localized.buttons,
        locale=locale, original_body=original_body,
        caution_level=caution_level)
