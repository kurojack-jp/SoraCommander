"""Owner-written task presets: named commands that ChatGPT can start by name.

The Owner writes %USERPROFILE%\\.config\\sora-commander\\tasks.json by hand. It is
read once when the MCP Host starts, never by an MCP tool or the Broker pipe.
A preset only fills in run_command_wait's arguments: every run still opens the
normal Owner approval window and follows the same policy, ledger and results.
A missing file means no tasks; any invalid content stops startup.
"""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat

from .commands import Command

MAX_TASKS_BYTES = 65536
MAX_TASKS = 32
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}\Z")
_KEYS = frozenset({"name", "description", "cwd", "executable", "argv",
                   "timeout", "output_limit"})


class TaskPresetError(ValueError):
    """tasks.json exists but is unusable; the message names no secret."""


@dataclass(frozen=True)
class TaskPreset:
    name: str
    description: str
    cwd: str
    executable: str
    argv: tuple[str, ...]
    timeout: float
    output_limit: int

    def public(self):
        return {"name": self.name, "description": self.description, "cwd": self.cwd,
                "executable": self.executable, "argv": list(self.argv),
                "timeout": self.timeout, "output_limit": self.output_limit}


def tasks_file(environ=None):
    environ = os.environ if environ is None else environ
    home = environ.get("USERPROFILE")
    if not home or not Path(home).is_absolute():
        return None  # No Owner profile, for example a non-Windows build check.
    return Path(home) / ".config" / "sora-commander" / "tasks.json"


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TaskPresetError("duplicate key")
        result[key] = value
    return result


def _plain_file(path):
    # Look at the spelling first so a junction or link anywhere is refused.
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise TaskPresetError("linked path")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise TaskPresetError("not an ordinary file")


def _text(value, limit):
    return (type(value) is str and 1 <= len(value) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _task(index, raw):
    where = f"task {index}"
    if type(raw) is not dict or set(raw) != _KEYS:
        raise TaskPresetError(f"{where}: keys must be exactly {', '.join(sorted(_KEYS))}")
    name = raw["name"]
    if type(name) is not str or not _NAME.fullmatch(name):
        raise TaskPresetError(f"{where}: invalid name")
    if not _text(raw["description"], 200):
        raise TaskPresetError(f"{where}: invalid description")
    argv = raw["argv"]
    if type(argv) is not list or any(type(item) is not str for item in argv):
        raise TaskPresetError(f"{where}: argv must be a list of strings")
    timeout = raw["timeout"]
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0.01 <= timeout <= 3600):
        raise TaskPresetError(f"{where}: timeout must be 0.01 to 3600")
    limit = raw["output_limit"]
    if type(limit) is not int or not 128 <= limit <= 65536:
        raise TaskPresetError(f"{where}: output_limit must be 128 to 65536")
    for key in ("cwd", "executable"):
        value = raw[key]
        if type(value) is not str or not Path(value).is_absolute():
            raise TaskPresetError(f"{where}: {key} must be an absolute path")
    try:
        # Same checks as a real request, including the .bat/.cmd argument rule.
        Command(executable=raw["executable"], argv=tuple(argv), cwd=raw["cwd"],
                timeout=float(timeout), output_limit=limit)
    except (ValueError, OSError) as exc:
        raise TaskPresetError(f"{where}: not a valid command") from exc
    return TaskPreset(name, raw["description"], raw["cwd"], raw["executable"],
                      tuple(argv), float(timeout), limit)


def load_task_presets(environ=None):
    """Return the validated presets as a tuple; () when the file is absent."""
    path = tasks_file(environ)
    if path is None:
        return ()
    try:
        path.lstat()
    except FileNotFoundError:
        # A missing file is fine, but never through a linked parent folder.
        for parent in reversed(path.parents):
            try:
                info = parent.lstat()
            except FileNotFoundError:
                break
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise TaskPresetError("linked path") from None
        return ()
    except OSError:
        raise TaskPresetError("file could not be read") from None
    try:
        _plain_file(path)
        with path.open("rb") as stream:
            data = stream.read(MAX_TASKS_BYTES + 1)
    except OSError:
        raise TaskPresetError("file could not be read") from None
    if len(data) > MAX_TASKS_BYTES:
        raise TaskPresetError("file is larger than 64 KiB")
    try:
        document = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_no_duplicates)
    except UnicodeDecodeError:
        raise TaskPresetError("file is not UTF-8") from None
    except TaskPresetError:
        raise
    except ValueError:
        raise TaskPresetError("file is not valid JSON") from None
    if type(document) is not dict or set(document) != {"tasks"}:
        raise TaskPresetError('top level must be exactly {"tasks": [...]}')
    raw_tasks = document["tasks"]
    if type(raw_tasks) is not list or not 1 <= len(raw_tasks) <= MAX_TASKS:
        raise TaskPresetError(f"tasks must be a list of 1 to {MAX_TASKS}")
    tasks = tuple(_task(index, raw) for index, raw in enumerate(raw_tasks, 1))
    if len({task.name for task in tasks}) != len(tasks):
        raise TaskPresetError("task names must be unique")
    return tasks
