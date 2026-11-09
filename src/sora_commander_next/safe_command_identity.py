"""Conservative, data-only command identity for Owner-private recovery.

Only literal known-safe invocations may be persisted verbatim. Unknown syntax,
scripts, -c/-Command, credential flags, arbitrary positional arguments, and
file contents are NEVER reconstructed or written to the archive. New commands
not on this list remain identified solely by their existing request/session IDs.
"""
import os
from pathlib import PurePath
import re
import subprocess

# This is deliberately NOT a general secret scrubber. Unrecognized commands
# must be absent rather than heuristically "redacted" into an unsafe transcript.
_SAFE_ARGV = {
    "python.exe": frozenset({("--version",), ("-V",), ("-VV",)}),
    "python3.exe": frozenset({("--version",), ("-V",), ("-VV",)}),
    "git.exe": frozenset({("--version",), ("status", "--short"),
                          ("status", "--porcelain")}),
    "node.exe": frozenset({("--version",), ("-v",)}),
}
_BLOCKED_PATH = re.compile(
    r"(?i)(?:sk-(?:proj-)?[a-z0-9_-]{8,}|api[_-]?key|secret|token|password|bearer|"
    r"\.env(?:[\\/]|$)|\.pem(?:[\\/]|$)|\.key(?:[\\/]|$))"
)


def safe_command_identity(executable, argv, cwd):
    """Return complete exact argv + cwd only for strictly known-safe forms.

    The archive format and recovery response use this same predicate; an
    injected 'safe_command' value cannot bypass the non-secret allowlist.
    """
    if (type(executable) is not str or type(cwd) is not str
            or not executable or not cwd
            or type(argv) not in (tuple, list) or not all(type(a) is str for a in argv)
            or len(executable) > 1024 or len(cwd) > 1024
            or "\0" in executable or "\0" in cwd):
        return None
    if not (os.path.isabs(executable) and os.path.isabs(cwd)):
        return None
    if _BLOCKED_PATH.search(executable) or _BLOCKED_PATH.search(cwd):
        return None
    base = PurePath(executable).name.lower()
    if os.name == "nt":
        from pathlib import PureWindowsPath
        base = PureWindowsPath(executable).name.lower()
    if tuple(argv) not in _SAFE_ARGV.get(base, ()):
        return None
    # Stored as structured, exact execution data; never reconstruct scripts,
    # shell input, command output, file contents or environment variables.
    return {"executable": executable, "argv": list(argv), "cwd": cwd}


def display_safe_command(identity):
    """Quote an already-approved exact native command, not a shell script."""
    if (type(identity) is not dict or
            set(identity) != {"executable", "argv", "cwd"}):
        return None
    verified = safe_command_identity(identity["executable"],
                                     identity["argv"], identity["cwd"])
    if verified is None or verified != identity:
        return None
    return subprocess.list2cmdline([verified["executable"], *verified["argv"]])
