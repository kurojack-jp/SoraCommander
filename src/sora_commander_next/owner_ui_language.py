"""Language of the local Owner review windows: en or ja.

Order: the Owner's hand-written language.txt, else the Windows display
language (Japanese -> ja, anything else -> en). Read once per process, so a
change needs a restart of the Broker and its client, like allowed-roots.txt.
The value only selects window text. It never changes approval, nonce,
expiry, Owner identity or any other safety check.
"""
import os
from pathlib import Path
import stat
from threading import Lock

SUPPORTED = ("en", "ja")
MAX_LANGUAGE_BYTES = 256
_LANG_JAPANESE = 0x11  # Windows primary language ID for Japanese.


class OwnerLanguageError(ValueError):
    """language.txt exists but is unusable. Startup should stop and say so."""


def language_file(environ=None):
    environ = os.environ if environ is None else environ
    home = environ.get("USERPROFILE")
    if not home or not Path(home).is_absolute():
        return None  # No Owner profile (for example a non-Windows build check).
    return Path(home) / ".config" / "sora-commander" / "language.txt"


def _windows_ui_language():
    """ja when the Windows display language is Japanese, otherwise en."""
    try:
        import ctypes
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except (AttributeError, OSError, ImportError):
        return "en"
    return "ja" if (int(langid) & 0x3FF) == _LANG_JAPANESE else "en"


def _parse(data):
    if len(data) > MAX_LANGUAGE_BYTES:
        raise OwnerLanguageError("language.txt is too large")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise OwnerLanguageError("language.txt is not UTF-8") from None
    values = [line.strip() for line in text.splitlines()
              if line.strip() and not line.strip().startswith("#")]
    if len(values) != 1 or values[0] not in SUPPORTED:
        raise OwnerLanguageError("language.txt must hold exactly one line: en or ja")
    return values[0]


def resolve_owner_ui_language(environ=None, *, windows_language=_windows_ui_language):
    """Return (locale, source) with source "file" or "windows"."""
    path = language_file(environ)
    if path is None:
        return windows_language(), "windows"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return windows_language(), "windows"
    except OSError:
        raise OwnerLanguageError("language.txt could not be read") from None
    if (not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise OwnerLanguageError("language.txt must be an ordinary file")
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_LANGUAGE_BYTES + 1)
    except OSError:
        raise OwnerLanguageError("language.txt could not be read") from None
    return _parse(data), "file"


_lock = Lock()
_cached = None


def owner_ui_language():
    """The process-wide window language, decided on first use and kept."""
    global _cached
    with _lock:
        if _cached is None:
            _cached = resolve_owner_ui_language()
        return _cached[0]


def owner_ui_language_source():
    owner_ui_language()
    return _cached[1]
