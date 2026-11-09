"""Single presentation-only renderer for LOCAL Owner review windows.

A distinct root and callbacks belong to each existing security adapter.
This module imports no tkinter, creates no windows on import, performs no
authentication, waits, process launch, file IO, consent or result mapping.
"""
from dataclasses import dataclass

_KINDS = frozenset({"command", "file_create", "file_edit", "v2_candidate"})
_REPEAT = "10-minute repeat permission (not enabled yet)"


@dataclass(frozen=True, slots=True)
class ReviewScreen:
    kind: str
    title: str
    heading: str
    caution: str
    information: str
    body: str
    trusted_offered: bool = False

    def __post_init__(self):
        if type(self.kind) is not str or self.kind not in _KINDS:
            raise ValueError("unknown Owner review presentation kind")
        for field in ("title", "heading", "caution", "information", "body"):
            value = getattr(self, field)
            if type(value) is not str or not value:
                raise ValueError("complete review presentation text required")
        if type(self.trusted_offered) is not bool:
            raise TypeError("trusted_offered must be bool")
        if self.kind != "v2_candidate" and self.trusted_offered:
            raise ValueError("reusable choice is forbidden in active Owner reviews")


def render_review(tk, root, theme, screen: ReviewScreen, *,
                  reject, approve, trusted=None):
    """Create the ONE shared visual layout, returning its read-only text widget.

    Button callbacks are externally supplied by the exact owning adapter,
    never created from request text. The renderer has no security authority.
    Host-based and isolated-child adapters retain separate roots, timeouts,
    wire formats, session checks and fail-closed decision state.
    """
    if type(screen) is not ReviewScreen:
        raise TypeError("exact ReviewScreen required")
    if not callable(reject) or not callable(approve):
        raise TypeError("two explicit callback functions required")
    if screen.kind == "v2_candidate":
        if screen.trusted_offered and not callable(trusted):
            raise TypeError("enabled trusted review requires a callback")
        if not screen.trusted_offered and trusted is not None:
            raise ValueError("disabled trusted review cannot retain a callback")
    elif trusted is not None:
        raise ValueError("trusted callback forbidden in active Owner reviews")

    root.title(screen.title)
    theme.window(root)
    card = theme.header(tk, root, screen.heading)
    theme.notice(tk, card, screen.caution, caution=True)
    theme.notice(tk, card, screen.information)
    pane = theme.viewer(tk, card)
    pane.insert("1.0", screen.body)
    pane.configure(state="disabled")
    bar = theme.button_bar(tk, card)
    deny = theme.neon_button(tk, bar, column=0, text="Reject", command=reject)
    theme.neon_button(
        tk, bar, column=1,
        text="Approve once" if screen.kind == "v2_candidate" else "Approve ONCE",
        command=approve, primary=True,
    )
    if screen.kind == "v2_candidate":
        enabled = screen.trusted_offered
        button = {"column": 2, "text": "Approve this command until revoked",
                  "disabled": not enabled}
        if enabled:
            button["command"] = trusted
        theme.neon_button(tk, bar, **button)
    else:
        theme.neon_button(tk, bar, column=2, text=_REPEAT, disabled=True)
    deny.focus_set()
    return pane
