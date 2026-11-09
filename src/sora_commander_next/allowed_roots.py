"""Startup-only local Owner scope. No tool, wire operation, or reload API."""
import os
from pathlib import Path
import stat

from .file_reads import _read_name_denied

MAX_ROOTS = 16
MAX_CONFIG_BYTES = 65536


class AllowedRootsError(ValueError):
    """Only a line number and a fixed reason; never include supplied paths."""

    def __init__(self, line, reason):
        self.line, self.reason = line, reason
        super().__init__(f"allowed-roots line {line}: {reason}")


def _plain_path(path, *, directory):
    # Inspect the original spelling before resolve can hide a junction.
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked path")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("wrong object type")
    if not directory and info.st_nlink != 1:
        raise ValueError("linked file")
    return path.resolve(strict=True)


def _absolute(value):
    path = Path(value)
    if (not path.is_absolute() or path.drive.startswith("\\\\")
            or value.startswith(("\\\\", "//"))
            or any(c in value for c in ('\x00', '"', '<', '>', '|', '?', '*'))
            or any(part in (".", "..") for part in value.replace("\\", "/").split("/"))):
        raise ValueError("absolute local path required")
    return path


def _within(path, parent):
    return path == parent or parent in path.parents


def _locations(environ):
    # The local launch environment is never supplied by MCP. Tests use fake
    # USERPROFILE/LOCALAPPDATA and OS paths, not the real Owner configuration.
    home = _absolute(environ["USERPROFILE"])
    local = _absolute(environ["LOCALAPPDATA"])
    drive = environ.get("SystemDrive", "C:")
    defaults = ("Windows", "Program Files", "Program Files (x86)", "ProgramData")
    blocked = [Path(drive + "\\") / name for name in defaults]
    for key in ("SystemRoot", "WINDIR", "ProgramFiles", "ProgramFiles(x86)",
                "ProgramW6432", "ProgramData"):
        if environ.get(key):
            blocked.append(_absolute(environ[key]))
    blocked.append(home / "AppData")
    config = home / ".config" / "sora-commander" / "allowed-roots.txt"
    contained = (config, home / ".config" / "tunnel-client", local / "SoraCommanderNext")
    return home, config, tuple(blocked), contained


def _validate_root(value, line, home, blocked, contained):
    reason = "absolute local path required"
    try:
        path = _absolute(value)
        if path == Path(path.anchor):
            reason = "drive root refused"
            raise ValueError("drive root")
        if any(_read_name_denied(part) for part in path.parts[1:]):
            reason = "protected path name"
            raise ValueError("protected name")
        if path == home or any(_within(path, item) for item in blocked):
            reason = "protected system or user folder"
            raise ValueError("protected folder")
        if (any(_within(item, path) for item in contained)
                or any(_within(path, item) for item in contained[1:])):
            reason = "contains private configuration or state"
            raise ValueError("contains private configuration or state")
        reason = "folder missing, inaccessible, linked or not a directory"
        resolved = _plain_path(path, directory=True)
        # Also check the canonical spelling (including DOS short-name aliases).
        if (any(_read_name_denied(part) for part in resolved.parts[1:])
                or resolved == home
                or any(_within(resolved, item) for item in blocked)
                or any(_within(item, resolved) for item in contained)
                or any(_within(resolved, item) for item in contained[1:])):
            reason = "protected canonical folder"
            raise ValueError("protected canonical folder")
        return str(resolved)
    except (OSError, ValueError, RuntimeError):
        raise AllowedRootsError(line, reason) from None


def startup_allowed_roots(repo_root, profile, environ=None):
    """Read once per Host composition; return an immutable validated snapshot.

    No cache or callable reload is attached to a Host. Restart the Broker and
    client together after an Owner edits the file. Line 0 denotes file-level
    errors or the fallback checkout, rather than an entry in the file.
    """
    if profile not in ("tests", "development"):
        raise AllowedRootsError(0, "unsupported local profile")
    base = Path(repo_root).resolve(strict=True)
    if profile == "tests":
        root = base / "tests"
        if not root.is_dir():
            raise AllowedRootsError(0, "test root missing")
        return (str(root),)
    environ = os.environ if environ is None else environ
    try:
        home, config, blocked, contained = _locations(environ)
        # Missing parents mean no file. Other errors, including access denied,
        # never become a missing-file fallback. Dangling links are not missing.
        try:
            config.lstat()
        except FileNotFoundError:
            # Still refuse any existing linked ancestor, including dangling ones.
            for parent in reversed(config.parents):
                try:
                    info = parent.lstat()
                except FileNotFoundError:
                    break
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise AllowedRootsError(0, "linked configuration path")
            return (_validate_root(str(base), 0, home, blocked, contained),)
        _plain_path(config, directory=False)
        with config.open("rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise AllowedRootsError(0, "configuration too large")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            line = data[:error.start].count(b"\n") + 1
            raise AllowedRootsError(line, "invalid UTF-8") from None
        roots = []
        for line, value in enumerate(text.splitlines(), 1):
            value = value.strip()
            if not value or value.startswith("#"):
                continue
            if len(roots) == MAX_ROOTS:
                raise AllowedRootsError(line, "more than 16 folders")
            roots.append(_validate_root(value, line, home, blocked, contained))
        if not roots:
            raise AllowedRootsError(0, "no folders configured")
        return tuple(roots)
    except AllowedRootsError:
        raise
    except (OSError, ValueError, KeyError, RuntimeError):
        raise AllowedRootsError(0, "configuration unavailable or invalid") from None
