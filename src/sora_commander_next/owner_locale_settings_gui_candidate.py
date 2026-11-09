"""Inactive, injected NON-SECRET synthetic Tk Settings. No approval callback API."""
import hashlib
import json
from pathlib import Path
import stat

from .owner_review_locale_settings_candidate import SyntheticLocaleSettings
from .approval_visual_theme import COLORS, button_options

_PINS = {"en": "ecb852c881b893cdcec6c7821a0f7dcb374ed7f0ce3a841356d0855b6aab35f9", "ja": "f2116548507d196d141fb2bcc0e79ea8213fafdc34ae64050c3cbe252c145275"}
_KEYS = frozenset(("title", "notice", "language", "current", "english",
                   "japanese", "save", "cancel", "error"))


def _catalog(locale):
    path = Path(__file__).absolute().with_name(f"settings_{locale}.dev.json")
    for parent in path.parents:
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("linked Settings resources")
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("unsafe Settings resource")
    with path.open("rb") as source:
        raw = source.read(4097)
    if len(raw) > 4096 or hashlib.sha256(raw).hexdigest() != _PINS[locale]:
        raise ValueError("Settings resource integrity failure")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate Settings label")
            result[key] = value
        return result
    labels = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    if (type(labels) is not dict or labels.keys() != _KEYS
            or any(type(v) is not str or not v or len(v) > 300 for v in labels.values())
            or labels["current"].count("{locale}") != 1):
        raise ValueError("invalid Settings labels")
    return labels


def settings_labels(locale="en"):
    if type(locale) is not str or locale not in ("en", "ja"):
        raise ValueError("unsupported Settings locale")
    english = _catalog("en")  # Never trust Japanese without verified base text.
    if locale == "en":
        return english
    try:
        return _catalog("ja")
    except FileNotFoundError:
        return english


def _screen_minimum(width, height):
    """Tk logical pixels only; invalid/mock dimensions keep legacy defaults."""
    def bounded(value, default):
        if type(value) is not int or value <= 0:
            return default
        return min(default, max(1, value - 64))
    return bounded(width, 460), bounded(height, 310)


class LocaleSettingsDialog:
    """Construct only from trusted DEV bootstrap, never from request fields.

    Call window.wait_window() if the bootstrap needs a modal wait. No global
    grab, implicit Tk root, app registration or live preference path is created.
    Catalog errors at construction raise BEFORE creating any window or Save.
    """
    def __init__(self, parent, *, settings):
        if type(settings) is not SyntheticLocaleSettings:
            raise TypeError("injected synthetic settings required")
        selected = settings.selected_locale()
        labels = settings_labels(selected)
        import tkinter as tk
        self._settings = settings
        self._labels = labels
        self._saved_locale = selected
        self._closed = False
        self._failed = False
        self.window = tk.Toplevel(parent)
        self.window.title(labels["title"])
        self.window.configure(background=COLORS["background"])
        self.window.minsize(*_screen_minimum(
            self.window.winfo_screenwidth(), self.window.winfo_screenheight()))
        self.window.resizable(True, True)
        self.window.transient(parent)
        self.choice = tk.StringVar(master=self.window, value=selected)
        card = tk.Frame(self.window, background=COLORS["card"], padx=20, pady=16)
        card.pack(fill="both", expand=True, padx=12, pady=12)
        def label(text, color="text"):
            widget = tk.Label(card, text=text, background=COLORS["card"],
                              foreground=COLORS[color], anchor="w", justify="left",
                              wraplength=440, font=("Segoe UI", 11))
            widget.pack(fill="x", pady=5)
            widget.bind("<Configure>", lambda e: widget.configure(wraplength=max(1, e.width)))
            return widget
        self.notice = label(labels["notice"], "cyan")
        self.current = label(labels["current"].format(locale=selected))
        self.language = label(labels["language"])
        self.options = []
        for value, key in (("en", "english"), ("ja", "japanese")):
            radio = tk.Radiobutton(card, text=labels[key], variable=self.choice,
                                   command=self.preview,
                                   value=value, background=COLORS["card"],
                                   foreground=COLORS["text"], selectcolor=COLORS["viewer"],
                                   anchor="w", justify="left", wraplength=440,
                                   activebackground=COLORS["card"],
                                   activeforeground=COLORS["cyan"], font=("Segoe UI", 11))
            radio.pack(fill="x", anchor="w", pady=4)
            radio.bind("<Configure>", lambda e, widget=radio: widget.configure(
                wraplength=max(1, e.width - 32)))
            self.options.append(radio)
        self.error = label("", "amber")
        actions = tk.Frame(card, background=COLORS["card"])
        actions.pack(fill="x", pady=(12, 0))
        self.cancel_button = tk.Button(actions, text=labels["cancel"], command=self.cancel,
                                       **button_options())
        self.cancel_button.pack(side="right", padx=4)
        self.save_button = tk.Button(actions, text=labels["save"], command=self.save,
                                     **button_options(primary=True))
        self.save_button.pack(side="right", padx=4)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.bind("<Control-s>", self._save_shortcut)
        self.window.bind("<Escape>", self.cancel)
        self.cancel_button.focus_set()

    def _save_shortcut(self, event):
        self.save()
        return "break"

    def preview(self):
        """Preview verified captions only; the saved value stays unchanged."""
        if self._closed or self._failed:
            return False
        try:
            labels = settings_labels(self.choice.get())
        except (OSError, ValueError):
            self._failed = True
            self.save_button.configure(state="disabled")
            self.error.configure(text=self._labels["error"])
            import sys
            if isinstance(sys.exception(), PermissionError):
                raise
            return False
        self.window.title(labels["title"])
        self.notice.configure(text=labels["notice"])
        self.current.configure(text=labels["current"].format(locale=self._saved_locale))
        self.language.configure(text=labels["language"])
        for radio, key in zip(self.options, ("english", "japanese")):
            radio.configure(text=labels[key])
        self.save_button.configure(text=labels["save"])
        self.cancel_button.configure(text=labels["cancel"])
        self._labels = labels
        return True

    def save(self):
        if self._closed or self._failed:
            return False
        try:
            settings_labels(self.choice.get())  # Revalidate before writing.
            self._settings.select_locale(self.choice.get())
        except (OSError, ValueError):
            self._failed = True
            self.save_button.configure(state="disabled")
            self.error.configure(text=self._labels["error"])
            # Real permission denials remain visible to the native caller.
            import sys
            if isinstance(sys.exception(), PermissionError):
                raise
            return False
        self.cancel()
        return True

    def cancel(self, event=None):
        if not self._closed:
            self._closed = True
            self.window.destroy()
        return "break"
