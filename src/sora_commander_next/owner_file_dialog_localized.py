"""Localized Owner file approval child. NOT ACTIVE: nothing connected launches it yet.

Private stdin/stdout only. It draws one prepared screen with the command window's
checked drawing and returns APPROVE, DENY or EXPIRED bound to the nonce. It holds
no file path to act on, writes nothing and decides nothing else. The parent owns
Owner identity, session checks, the proposal and the write.
"""
import json
import re
import sys

MAX_REQUEST = 1048576
_FIELDS = frozenset({"nonce", "seconds", "locale", "kind", "title", "heading", "caution",
                     "information", "body", "labels"})
_TEXT_FIELDS = ("title", "heading", "caution", "information", "body")
_KINDS = ("file_create", "file_edit")
_LOCALES = ("en", "ja")
_DEFAULT_CAPTIONS = ("Reject", "Approve ONCE", "10-minute repeat permission (not enabled yet)")


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate field")
        result[name] = value
    return result


def parse_request(raw):
    if type(raw) is not bytes or len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
        raise ValueError("invalid private UI request")
    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    if (type(payload) is not dict
            or payload.keys() != _FIELDS
            or type(payload["nonce"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", payload["nonce"]) is None
            or type(payload["seconds"]) not in (int, float)
            or not 0 < payload["seconds"] <= 60
            or payload["locale"] not in _LOCALES
            or payload["kind"] not in _KINDS
            or any(type(payload[name]) is not str or not payload[name] or "\x00" in payload[name]
                   for name in _TEXT_FIELDS)
            or type(payload["labels"]) is not list
            or len(payload["labels"]) != 3
            or any(type(label) is not str or not label for label in payload["labels"])):
        raise ValueError("invalid UI request fields")
    return payload


def _load_command_window():
    """The active command child's checked drawing, loaded under python -I without sys.path."""
    import importlib.util
    from pathlib import Path
    location = Path(__file__).resolve().with_name("owner_command_dialog_localized.py")
    name = "_sora_file_window_command_child"
    spec = importlib.util.spec_from_file_location(name, location)
    if spec is None or spec.loader is None:
        raise RuntimeError("trusted Owner review module unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def main():
    request = parse_request(sys.stdin.buffer.readline(MAX_REQUEST + 1))
    drawing = _load_command_window()
    theme = drawing._load_sibling("approval_visual_theme.py")
    renderer = drawing._load_sibling("owner_review_renderer.py")
    screen = renderer.ReviewScreen(
        kind=request["kind"], title=request["title"], heading=request["heading"],
        caution=request["caution"], information=request["information"], body=request["body"])
    labels = dict(enumerate(zip(_DEFAULT_CAPTIONS, request["labels"])))
    import tkinter as tk
    selected = {"decision": None}
    root = tk.Tk()

    def decide(value):
        # Tk callbacks run serially; the first event owns the decision.
        if selected["decision"] is not None:
            return
        selected["decision"] = value
        root.destroy()

    drawing.render_checked(tk, root, theme, renderer, screen, labels, locale=request["locale"],
                           reject=lambda: decide("DENY"), approve=lambda: decide("APPROVE"))
    root.protocol("WM_DELETE_WINDOW", lambda: decide("DENY"))
    root.after(int(request["seconds"] * 1000), lambda: decide("EXPIRED"))
    root.attributes("-alpha", 1.0)

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
