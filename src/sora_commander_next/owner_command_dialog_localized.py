"""Localized Owner command approval child. NOT ACTIVE: no broker launches it yet.

Same private stdin/stdout role and APPROVE/DENY/EXPIRED reply as the active
owner_approval_dialog.py, plus a locale chosen once by the broker. The request
cannot name a locale source. Language and the caution indicator change display
text only; neither reaches the reply, a callback or a button state.
Owner identity, nonce checks, session lock and launch stay in the broker.
"""
import json
import re
import sys

_FIELDS = frozenset({"nonce", "executable", "argv", "cwd", "seconds", "pending_id",
                     "timeout", "output_limit", "locale"})
_LOCALES = ("en", "ja")
# Exact default renderer captions. An unexpected caption stops the review.
_RENDERER_CAPTIONS = {
    0: ("Reject", "reject"),
    1: ("Approve ONCE", "approve_once"),
    2: ("10-minute repeat permission (not enabled yet)", "repeat_disabled"),
}
# The window is not shown unless this many lines of the command are visible.
_MIN_BODY_LINES = 5
_VIEWER_PADDING = 20  # the theme's top and bottom text padding
_COMPACT_BUTTON_HEIGHT = 58
_COMPACT_NOTICE_LINES = 3
_SCREEN_RESERVED = 80  # taskbar and title bar
# Looks only. A missing font keeps the theme's font and changes nothing else.
_THEME_UI_FONT = "Segoe UI"
_JAPANESE_UI_FONT = "Yu Gothic UI"  # Owner's choice from real renderings, 2026-11-04
_JAPANESE_BODY_FONTS = ("BIZ UDGothic", "BIZ UDゴシック")
_THEME_BODY_FONT = ("Consolas", 11)
_JAPANESE_BODY_SIZE = 12
_JAPANESE_NOTICE_SIZE = 11
_THEME_NOTICE_SIZE = 10
_WIDE_TEXT = re.compile(r"[^\x00-\x7f]+")


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate field")
        result[name] = value
    return result


def parse_request(raw):
    if type(raw) is not bytes or len(raw) > 131072 or not raw.endswith(b"\n"):
        raise ValueError("invalid private UI request")
    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    if (type(payload) is not dict
            or payload.keys() != _FIELDS
            or type(payload["nonce"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", payload["nonce"]) is None
            or type(payload["executable"]) is not str
            or not payload["executable"]
            or type(payload["argv"]) is not list
            or any(type(v) is not str for v in payload["argv"])
            or type(payload["cwd"]) is not str
            or type(payload["pending_id"]) is not str
            or type(payload["seconds"]) not in (int, float)
            or not 0 < payload["seconds"] <= 60
            or type(payload["timeout"]) not in (int, float)
            or type(payload["output_limit"]) is not int
            or type(payload["locale"]) is not str
            or payload["locale"] not in _LOCALES):
        raise ValueError("invalid UI request fields")
    return payload


def command_body(request, words, fill):
    """Labels come from the catalog; every value keeps the active child's exact form.

    English output equals the active child's text; a test compares the two.
    """
    return (fill(words["body_executable"], repr(request["executable"])) + "\n"
            + fill(words["body_argv"], repr(request["argv"])) + "\n"
            + fill(words["body_cwd"], repr(request["cwd"])) + "\n"
            + fill(words["body_timeout"], str(request["timeout"])) + "\n"
            + fill(words["body_output_limit"], str(request["output_limit"])) + "\n"
            + fill(words["body_review_id"], repr(request["pending_id"])) + "\n"
            + fill(words["body_expires"], f"{request['seconds']:.0f}") + "\n"
            + words["body_unknown_effects"] + "\n"
            + words["body_not_remote"] + "\n")


def _load_sibling(filename):
    """Load one fixed sibling under python -I, without changing sys.path."""
    import importlib.util
    from pathlib import Path
    location = Path(__file__).resolve().with_name(filename)
    name = "_sora_localized_" + filename.removesuffix(".py")
    spec = importlib.util.spec_from_file_location(name, location)
    if spec is None or spec.loader is None:
        raise RuntimeError("trusted Owner review module unavailable")
    module = importlib.util.module_from_spec(spec)
    # dataclass resolves its declaring module during class creation.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def build_screen(request, renderer, i18n, caution):
    """Build all display data from one parsed request; any failure stops the UI."""
    locale = request["locale"]
    words = i18n.load_catalog(locale)
    advice = caution.command_caution(
        executable=request["executable"], argv=list(request["argv"]), locale=locale)
    notice = "\n".join((words["caution"], *advice.display_lines()))
    screen = renderer.ReviewScreen(
        kind="command", title=words["window_title"], heading=words["heading_command"],
        caution=notice, information=words["command_close_expiry_notice"],
        body=command_body(request, words, i18n.fill))
    labels = {column: (default, words[key])
              for column, (default, key) in _RENDERER_CAPTIONS.items()}
    return screen, labels


def _presentation(theme, labels, parts=None):
    """The shared theme with translated captions; every other argument is unchanged.

    `parts` only remembers what was drawn, so the layout check can tighten it.
    """
    from types import SimpleNamespace
    parts = parts if parts is not None else {"notices": [], "buttons": []}

    def notice(tk, parent, text, **options):
        label = theme.notice(tk, parent, text, **options)
        parts["notices"].append((parent, label, text))
        return label

    def neon_button(tk, parent, *, column, text, **options):
        if type(column) is not int or column not in labels or labels[column][0] != text:
            raise ValueError("unexpected Owner button mapping")
        button = theme.neon_button(tk, parent, column=column, text=labels[column][1], **options)
        parts["buttons"].append(button)
        return button

    return SimpleNamespace(
        window=theme.window, header=theme.header, notice=notice, viewer=theme.viewer,
        button_bar=theme.button_bar, neon_button=neon_button)


def _body_lines(root, pane):
    root.update()
    spacing = root.tk.call("font", "metrics", pane.cget("font"), "-linespace")
    return (pane.winfo_height() - _VIEWER_PADDING) / spacing


def _style_body(pane, body, families, size):
    """Draw non-ASCII runs in a Japanese font. ASCII stays Consolas, so 0 and O stay distinct."""
    family = next((name for name in _JAPANESE_BODY_FONTS if name in families), None)
    if family is None:
        return
    pane.tag_configure("wide", font=(family, size))
    for number, line in enumerate(body.split("\n"), 1):
        for match in _WIDE_TEXT.finditer(line):
            pane.tag_add("wide", f"{number}.{match.start()}", f"{number}.{match.end()}")


def _style_labels(root, widget):
    """Japanese labels and buttons: the chosen family, one point larger, same weight."""
    for item in widget.winfo_children():
        if item.winfo_class() in ("Label", "Button"):
            font = tuple(root.tk.splitlist(item.cget("font")))
            if len(font) >= 2 and font[0] == _THEME_UI_FONT:
                item.configure(font=(_JAPANESE_UI_FONT, int(font[1]) + 1, *font[2:]))
        _style_labels(root, item)


def _style_fonts(root, pane, body, locale, parts):
    families = root.tk.call("font", "families")
    if locale != "ja":
        _style_body(pane, body, families, _THEME_BODY_FONT[1])
        return
    pane.configure(font=(_THEME_BODY_FONT[0], _JAPANESE_BODY_SIZE))
    _style_body(pane, body, families, _JAPANESE_BODY_SIZE)
    if _JAPANESE_UI_FONT in families:
        _style_labels(root, root)
        parts["notice_font"] = (_JAPANESE_UI_FONT, _JAPANESE_NOTICE_SIZE)


def _compact_notices(tk, root, theme, parts):
    """Move every notice word into one short scrolling box above the command."""
    parent = parts["notices"][0][0]
    text = "\n".join(entry[2] for entry in parts["notices"])
    for _, label, _ in parts["notices"]:
        label.destroy()
    holder = tk.Frame(parent, background=theme.COLORS["caution"])
    holder.pack(fill="x", padx=18, pady=(6, 0), before=parent._sora_viewer_panel)
    box = tk.Text(holder, height=_COMPACT_NOTICE_LINES, wrap="word",
                  font=parts.get("notice_font", (_THEME_UI_FONT, _THEME_NOTICE_SIZE)),
                  background=theme.COLORS["caution"], foreground=theme.COLORS["amber"],
                  relief="flat", borderwidth=0, padx=12, pady=4, exportselection=False)
    bar = tk.Scrollbar(holder, command=box.yview)
    box.configure(yscrollcommand=bar.set)
    box.insert("1.0", text)
    box.configure(state="disabled")
    bar.pack(side="right", fill="y")
    box.pack(side="left", fill="x", expand=True)


def _place(root, height=None):
    """Keep the whole window, buttons included, above the taskbar."""
    width = root.winfo_width()
    screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
    if height is None:
        height = root.winfo_height()
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - _SCREEN_RESERVED - height) // 2)
    root.geometry(f"{width}x{height}+{x}+{y}")


def _fit_review(tk, root, theme, pane, parts):
    """Never show Approve beside a command the Owner cannot read."""
    root.update()
    _place(root)
    if _body_lines(root, pane) >= _MIN_BODY_LINES:
        return
    for button in parts["buttons"]:
        button.master.configure(height=_COMPACT_BUTTON_HEIGHT)
    if _body_lines(root, pane) >= _MIN_BODY_LINES:
        return
    _compact_notices(tk, root, theme, parts)
    root.minsize(1, 1)
    _place(root, max(1, root.winfo_screenheight() - _SCREEN_RESERVED))
    if _body_lines(root, pane) < _MIN_BODY_LINES:
        raise RuntimeError("screen too small to show the Owner review body")


def render_checked(tk, root, theme, renderer, screen, labels, *, locale, reject, approve):
    """Draw while invisible, check the command is readable, and only then reveal it."""
    root.attributes("-alpha", 0.0)
    parts = {"notices": [], "buttons": []}
    pane = renderer.render_review(tk, root, _presentation(theme, labels, parts), screen,
                                  reject=reject, approve=approve)
    _style_fonts(root, pane, screen.body, locale, parts)
    _fit_review(tk, root, theme, pane, parts)
    return pane


def main():
    request = parse_request(sys.stdin.buffer.readline(131073))
    theme = _load_sibling("approval_visual_theme.py")
    renderer = _load_sibling("owner_review_renderer.py")
    screen, labels = build_screen(
        request, renderer,
        _load_sibling("owner_review_i18n.py"), _load_sibling("owner_command_caution.py"))
    import tkinter as tk
    selected = {"decision": None}
    root = tk.Tk()

    def decide(value):
        # Tk callbacks run serially; the first event owns the decision.
        if selected["decision"] is not None:
            return
        selected["decision"] = value
        root.destroy()

    render_checked(tk, root, theme, renderer, screen, labels, locale=request["locale"],
                   reject=lambda: decide("DENY"), approve=lambda: decide("APPROVE"))
    root.protocol("WM_DELETE_WINDOW", lambda: decide("DENY"))
    root.after(int(request["seconds"] * 1000), lambda: decide("EXPIRED"))
    root.attributes("-alpha", 1.0)
    # Non-blocking Windows system alert when the Owner review appears.
    # Audio failure must never approve, deny, or change the expiry clock.
    def alert_review():
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            try:
                root.bell()
            except Exception:
                pass
    root.after(100, alert_review)
    root.mainloop()
    if selected["decision"] is None:
        raise RuntimeError("Owner review ended without a decision or timer expiry")
    result = {"nonce": request["nonce"], "decision": selected["decision"]}
    sys.stdout.buffer.write(json.dumps(result).encode("ascii") + b"\n")
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
