"""DEV candidate: visual caution indicator for one Owner command review.

This is NOT a command safety classifier, approval policy, command validator
or execution gate. The only supported default is UNKNOWN, not SAFE.
Indicators describe detectable traits of the supplied launch text only.
No filesystem inspection, dynamic execution, Tk, GUI callbacks or writes.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import ntpath
from pathlib import Path
import re
from types import MappingProxyType

_CATALOG = MappingProxyType({
    "en": ("caution_en.json", "6b50d340e4969c3624d8f8bc050559a681d06d083259fc670cfd6a91e90867e4"),
    "ja": ("caution_ja.json", "30680e0aa376ca17a26336d3cdbf3ba9463e09139030e23715c47788db81c9d0"),
})
_KEYS = frozenset({"heading", "unknown", "attention", "strong",
                   "unknown_reason", "runtime", "nested", "encoded",
                   "system", "git_danger", "destructive", "disclaimer"})
_LEVELS = MappingProxyType({
    "UNKNOWN": ("❔", "unknown"),
    "ATTENTION": ("⚠️", "attention"),
    "STRONG": ("🔶", "strong"),
})
_INTERPRETERS = frozenset({
    "powershell.exe", "pwsh.exe", "cmd.exe", "python.exe", "pythonw.exe",
    "py.exe", "node.exe", "wscript.exe", "cscript.exe", "bash.exe",
    "sh.exe", "ruby.exe", "perl.exe",
})
_SHELLS = frozenset({"powershell.exe", "pwsh.exe", "cmd.exe", "bash.exe", "sh.exe"})
_SYSTEM = frozenset({"reg.exe", "sc.exe", "bcdedit.exe", "diskpart.exe",
                     "format.com", "icacls.exe", "takeown.exe", "cipher.exe"})
_ENCODED = frozenset({"-encodedcommand", "-enc", "-e"})
_SHELL_INLINE = frozenset({"-command", "-c", "/c", "/k", "-file"})
_DESTRUCTIVE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?:remove-item|stop-service|clear-disk|"
    r"format-volume|shutil\.rmtree|os\.remove|remove-computer|"
    r"reg\s+delete|sc\s+delete|drop\s+table)(?![A-Za-z0-9_-])"
)


def _pairs_no_duplicate(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate caution translation key")
        obj[key] = value
    return obj


def load_caution_catalog(locale):
    """Only two fixed, SHA-pinned files. No external JSON is selectable."""
    if type(locale) is not str or locale not in _CATALOG:
        raise ValueError("unsupported caution locale")
    name, expected = _CATALOG[locale]
    folder = Path(__file__).resolve().parent / "i18n"
    file = folder / name
    if folder.is_symlink() or file.is_symlink():
        raise ValueError("linked caution dictionary")
    data = file.read_bytes()
    if len(data) > 4096 or sha256(data).hexdigest() != expected:
        raise ValueError("caution dictionary integrity mismatch")
    try:
        values = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs_no_duplicate)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("invalid caution dictionary") from exc
    if type(values) is not dict or values.keys() != _KEYS:
        raise ValueError("unexpected caution dictionary shape")
    if any(type(value) is not str or not 1 <= len(value) <= 360
           or any(ord(char) < 32 for char in value)
           for value in values.values()):
        raise ValueError("unsafe caution translation")
    return dict(values)


@dataclass(frozen=True, slots=True)
class CommandCaution:
    """Localized display data only. Never pass 'level' to a policy function."""
    level: str
    icon: str
    title: str
    label: str
    reasons: tuple
    disclaimer: str
    # This is intentionally unbound to any Owner review response/choice.

    def display_lines(self):
        return (self.title + ": " + self.icon + " " + self.label,
                *self.reasons, self.disclaimer)


def command_caution(*, executable, argv, locale="ja"):
    """Classify only observable launch-text clues; NEVER report SAFE/LOW.

    The result cannot determine which program is actually on disk, resolve
    scripts, inspect dependencies or discover dynamically generated commands.
    Even UNKNOWN can perform destructive actions. Risk is not a percentage.
    """
    words = load_caution_catalog(locale)
    if type(executable) is not str or not 1 <= len(executable) <= 1024 or "\x00" in executable:
        raise ValueError("invalid executable for caution display")
    if (type(argv) is not list or len(argv) > 128
            or any(type(s) is not str or len(s) > 8192 or "\x00" in s for s in argv)):
        raise ValueError("invalid argument vector for caution display")
    basename = ntpath.basename(executable.replace("/", "\\")).casefold()
    flags = frozenset(arg.casefold() for arg in argv)
    joined = " ".join(argv)
    level = "UNKNOWN"
    reasons = []

    def add(level_hint, reason):
        nonlocal level
        if level_hint == "STRONG" or (level == "UNKNOWN" and level_hint == "ATTENTION"):
            level = level_hint
        if reason not in reasons:
            reasons.append(reason)

    if basename in _INTERPRETERS:
        add("ATTENTION", "runtime")
    if basename in _SYSTEM:
        add("ATTENTION", "system")
    if basename in _SHELLS and flags.intersection(_SHELL_INLINE):
        add("ATTENTION", "nested")
    if basename in ("powershell.exe", "pwsh.exe") and flags.intersection(_ENCODED):
        add("STRONG", "encoded")
    if basename in _SHELLS and _DESTRUCTIVE.search(joined):
        add("STRONG", "destructive")
    if basename in ("diskpart.exe", "format.com"):
        add("STRONG", "system")
    if basename in ("git.exe", "git"):
        if ("push" in flags or "--hard" in flags
                or any(a in flags for a in ("--force", "--force-with-lease", "-fdx", "-fd", "-df"))):
            add("STRONG", "git_danger")
    if basename in ("reg.exe", "sc.exe", "bcdedit.exe") and any(
            a in flags for a in ("delete", "/delete", "/deletevalue", "/deletekey")):
        add("STRONG", "destructive")

    icon, label_key = _LEVELS[level]
    return CommandCaution(
        level=level, icon=icon, title=words["heading"],
        label=words[label_key],
        reasons=tuple(words[k] for k in reasons) if reasons else (words["unknown_reason"],),
        disclaimer=words["disclaimer"],
    )
