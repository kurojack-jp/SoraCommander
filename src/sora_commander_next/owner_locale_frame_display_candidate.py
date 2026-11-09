"""Inactive DEV display snapshot. No decision, timer, wire or storage authority.

Only trusted bootstrap code injects a SyntheticLocaleSettings instance and a
kind. Neither a request nor its text can select a settings location. Deadline,
nonce, expiry and decision semantics remain entirely with the active child.
"""
from dataclasses import dataclass

from .owner_review_locale_settings_candidate import SyntheticLocaleSettings
from .owner_review_request_binding_candidate import bind_command_frame, bind_v2_frame
from .owner_review_presentation_candidate import ReviewPresentationCandidate


@dataclass(frozen=True, slots=True)
class LocaleFrameDisplay:
    frame_sha256: str
    pending_id: str
    presentation: ReviewPresentationCandidate

    @property
    def locale(self):
        return self.presentation.locale

    @property
    def original_body(self):
        return self.presentation.original_body

    @property
    def labels(self):
        return self.presentation.labels


def freeze_locale_frame(frame, *, settings, kind="command"):
    """Select locale ONCE and bind one byte-exact validated command/v2 frame.

    No caller-supplied body, locale, path, callbacks or trusted-choice default.
    v2's required allow_trusted is validated by the existing v2 parser/binder.
    The digest identifies bytes for display comparison, not authentication.
    """
    if type(settings) is not SyntheticLocaleSettings:
        raise TypeError("trusted injected synthetic settings required")
    if type(kind) is not str or kind not in ("command", "v2_candidate"):
        raise ValueError("unsupported synthetic display kind")
    locale = settings.selected_locale()
    binder = bind_command_frame if kind == "command" else bind_v2_frame
    bound = binder(frame, locale=locale)
    return LocaleFrameDisplay(bound.frame_sha256, bound.review_identity,
                              bound.presentation)
