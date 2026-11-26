"""Shared validation and bounded, explicit Owner configuration transactions."""
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from datetime import datetime
from difflib import ndiff
import os
from pathlib import Path
import stat
from uuid import uuid4

from sora_commander_next import allowed_roots, owner_ui_language, task_presets

NAMES = ("allowed-roots.txt", "language.txt", "tasks.json")
LIMITS = {NAMES[0]: allowed_roots.MAX_CONFIG_BYTES,
          NAMES[1]: owner_ui_language.MAX_LANGUAGE_BYTES,
          NAMES[2]: task_presets.MAX_TASKS_BYTES}


class ConfigError(ValueError):
    """Fixed messages only; file contents and command arguments are not logged."""


class SaveError(ConfigError):
    def __init__(self, completed, backups, error):
        self.completed = tuple(completed)
        self.backups = dict(backups)
        self.failure = (str(error) if isinstance(error, ConfigError) else
                        f"{type(error).__name__} / WinError {getattr(error, 'winerror', None)}")
        super().__init__("Save stopped. Inspect completed files and backups before any new save.")


@dataclass(frozen=True)
class FileRecord:
    data: bytes | None
    identity: tuple | None


def config_directory(environ):
    path = task_presets.tasks_file(environ)
    if path is None:
        raise ConfigError("USERPROFILE must be an absolute local folder.")
    if path.drive.startswith("\\\\"):
        raise ConfigError("A local Owner profile is required.")
    return path.parent


def _identity(info):
    # Windows path stat and handle stat can disagree on ctime for unchanged files.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _ordinary(info, directory):
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise ConfigError("Linked or non-ordinary configuration path refused.")


def inspect_parents(folder):
    for part in (*reversed(folder.parents), folder):
        try:
            _ordinary(part.lstat(), True)
        except FileNotFoundError:
            return


def read_record(path, limit):
    inspect_parents(path.parent)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return FileRecord(None, None)
    _ordinary(before, False)
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        _ordinary(opened, False)
        if _identity(before) != _identity(opened):
            raise ConfigError("Configuration changed while reading.")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if _identity(before) != _identity(after) or _identity(before) != _identity(path.lstat()):
        raise ConfigError("Configuration changed while reading.")
    if len(data) > limit:
        raise ConfigError("Configuration is too large to edit safely.")
    return FileRecord(data, _identity(before))


def snapshot(environ):
    folder = config_directory(environ)
    return {name: read_record(folder / name, LIMITS[name]) for name in NAMES}


def _fallback_roots(root, environ):
    home, _, blocked, contained = allowed_roots._locations(environ)
    value = allowed_roots._validate_root(str(Path(root).resolve(strict=True)), 0,
                                        home, blocked, contained)
    return allowed_roots.AllowedRoots((value,))


def validate_candidate(contents, root, environ):
    """Use core validators. Missing files retain core startup defaults."""
    if set(contents) != set(NAMES):
        raise ConfigError("Exactly the three supported configuration files are required.")
    results, roots, tasks = [], None, ()
    for name in NAMES:
        data = contents[name]
        if data is not None and type(data) is not bytes:
            raise ConfigError("Configuration content must be bytes or absent.")
        try:
            if name == NAMES[0]:
                roots = (_fallback_roots(root, environ) if data is None
                         else allowed_roots.parse_allowed_roots(data, environ))
                detail = f"{len(roots.full)} full, {len(roots.read_only)} read-only"
            elif name == NAMES[1]:
                detail = "Windows default" if data is None else owner_ui_language._parse(data)
            else:
                tasks = () if data is None else task_presets.parse_task_presets(data)
                detail = f"{len(tasks)} tasks"
            level = "WARN" if name == NAMES[0] and data is None else "PASS"
            results.append((level, name, detail))
            if name == NAMES[0]:
                results.extend(("WARN", name, f"allowed-roots line {line}: {allowed_roots.MISSING_FOLDER}")
                               for line in roots.skipped)
        except (ValueError, OSError, RuntimeError) as error:
            known = isinstance(error, (allowed_roots.AllowedRootsError,
                                      task_presets.TaskPresetError,
                                      owner_ui_language.OwnerLanguageError))
            results.append(("HOLD", name, str(error) if known else "Validation unavailable."))
    if roots is not None:
        for index, task in enumerate(tasks, 1):
            try:
                cwd = Path(task.cwd).resolve()
                inside = any(cwd.is_relative_to(Path(folder).resolve()) for folder in roots.full)
            except (OSError, RuntimeError):
                inside = False
            if not inside:
                results.append(("WARN", NAMES[2], f"task {index}: cwd outside full folders"))
    return results


def check_existing(root, environ=None):
    """Actual startup entry points, no caching, writes or GUI imports."""
    environ = dict(os.environ if environ is None else environ)
    results = []
    try:
        folder = config_directory(environ)
    except ConfigError as error:
        return [("HOLD", name, str(error)) for name in NAMES]
    for name in NAMES:
        try:
            record = read_record(folder / name, LIMITS[name])
            if name == NAMES[0]:
                roots = allowed_roots.startup_allowed_roots(root, "development", environ)
                detail = f"{len(roots.full)} full, {len(roots.read_only)} read-only"
            elif name == NAMES[1]:
                language, source = owner_ui_language.resolve_owner_ui_language(environ)
                detail = f"{language} ({source})"
            else:
                detail = f"{len(task_presets.load_task_presets(environ))} tasks"
            if record != read_record(folder / name, LIMITS[name]):
                raise ConfigError("Configuration changed while checking.")
            results.append(("WARN" if name == NAMES[0] and record.data is None else "PASS",
                            name, detail))
            if name == NAMES[0]:
                results.extend(("WARN", name, f"allowed-roots line {line}: {allowed_roots.MISSING_FOLDER}")
                               for line in roots.skipped)
        except (ValueError, OSError, RuntimeError) as error:
            known = isinstance(error, (ConfigError, allowed_roots.AllowedRootsError,
                                      task_presets.TaskPresetError,
                                      owner_ui_language.OwnerLanguageError))
            results.append(("HOLD", name, str(error) if known else "Configuration unavailable."))
    return results


def difference(before, after):
    old = (before or b"").decode("utf-8-sig", errors="replace").splitlines()
    new = (after or b"").decode("utf-8-sig", errors="replace").splitlines()
    lines = list(ndiff(old, new))
    return sum(line.startswith("+ ") for line in lines), sum(line.startswith("- ") for line in lines)


@contextmanager
def _pin_directory(path):
    """Windows handles prevent parent rename/reparse replacement during save."""
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    create = api.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = api.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    before = path.lstat()
    _ordinary(before, True)
    handle = create(str(path), 0x80, 3, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        current = path.lstat()
        _ordinary(current, True)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise ConfigError("Configuration directory changed.")
        yield
    finally:
        if not close(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def _write_new(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def save(contents, baseline, root, environ, *, acknowledged=False):
    """Per-file atomic replacement, verified backups, no implicit rollback/retry.

    A partial save reports exactly which replacements finished. Backups remain
    for Owner reconciliation. Cooperating Builder instances share one lock.
    """
    if os.name != "nt":
        raise ConfigError("Windows is required for saving.")
    if not acknowledged:
        raise ConfigError("The read-access warning must be acknowledged.")
    results = validate_candidate(contents, root, environ)
    if any(level == "HOLD" for level, _, _ in results):
        raise ConfigError("Fix validation errors before saving.")
    if snapshot(environ) != baseline:
        raise ConfigError("Configuration changed since it was loaded. Reopen the editor.")
    changed = [name for name in NAMES if contents[name] != baseline[name].data]
    if not changed:
        return {}, check_existing(root, environ)
    folder = config_directory(environ)
    completed, backups, temporary = [], {}, {}
    with ExitStack() as stack:
        # Only create the two known settings directories, after confirmation.
        home = Path(environ["USERPROFILE"])
        for part in (*reversed(home.parents), home):
            stack.enter_context(_pin_directory(part))
        for part in (home / ".config", folder):
            try:
                part.lstat()
            except FileNotFoundError:
                part.mkdir()
            stack.enter_context(_pin_directory(part))
        lock = folder / ".config-builder.lock"
        marker = uuid4().hex.encode("ascii")
        _write_new(lock, marker)  # Exclusive creation: busy/stale locks stop here.
        lock_record = read_record(lock, len(marker))
        try:
            if snapshot(environ) != baseline:
                raise ConfigError("Configuration changed before save.")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            for name in changed:
                previous = baseline[name].data
                if previous is not None:
                    backup = folder / f"{name}.bak-{stamp}"
                    _write_new(backup, previous)
                    if read_record(backup, LIMITS[name]).data != previous:
                        raise ConfigError("Backup verification failed.")
                    backups[name] = str(backup)
                if contents[name] is not None:
                    staged = folder / f".{name}.{uuid4().hex}.tmp"
                    temporary[staged] = contents[name]
                    _write_new(staged, contents[name])
                    if read_record(staged, LIMITS[name]).data != contents[name]:
                        raise ConfigError("Staged content verification failed.")
            if snapshot(environ) != baseline:
                raise ConfigError("Configuration changed during backup.")
            for name, backup in backups.items():
                if read_record(Path(backup), LIMITS[name]).data != baseline[name].data:
                    raise ConfigError("Backup changed before replacement.")
            for name in changed:
                target = folder / name
                if read_record(target, LIMITS[name]) != baseline[name]:
                    raise ConfigError("Configuration changed before replacement.")
                data = contents[name]
                if data is None:
                    target.unlink()
                else:
                    staged = next(path for path in temporary if path.name.startswith(f".{name}."))
                    if read_record(staged, LIMITS[name]).data != data:
                        raise ConfigError("Staged content changed before replacement.")
                    if baseline[name].data is None:
                        os.rename(staged, target)  # Windows refuses an unexpected existing target.
                    else:
                        os.replace(staged, target)
                    del temporary[staged]
                completed.append(name)
                if read_record(target, LIMITS[name]).data != data:
                    raise ConfigError("Written content changed.")
            if {name: record.data for name, record in snapshot(environ).items()} != contents:
                raise ConfigError("Configuration changed after replacement.")
            results = check_existing(root, environ)
            if any(level == "HOLD" for level, _, _ in results):
                raise ConfigError("Startup validation failed after saving.")
            # A replaced lock or failed removal must not become a successful
            # save report. Preserve completed names/backups in SaveError.
            if read_record(lock, len(marker)) != lock_record:
                raise ConfigError("Save lock changed before completion.")
            lock.unlink()
            return backups, results
        except (OSError, ValueError, RuntimeError) as error:
            # Retain remaining evidence; never retry cleanup after a denial.
            raise SaveError(completed, backups, error) from error
