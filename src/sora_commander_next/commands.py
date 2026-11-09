"""Immutable, normalized requests; no shell strings or environment overrides."""

from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4
import math


class BatchArgumentMetacharacterRejected(ValueError):
    """Batch files cannot safely receive cmd.exe metacharacters as arguments."""


def absolute(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("path must be a nonempty string")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("absolute path required; executable aliases are unsupported")
    return str(path.resolve())


@dataclass(frozen=True)
class Command:
    executable: str
    argv: tuple[str, ...]
    cwd: str
    timeout: float = 5.0
    output_limit: int = 8192
    request_id: str = field(default_factory=lambda: uuid4().hex)
    interactive: bool = False

    def __post_init__(self):
        if type(self.interactive) is not bool:
            raise ValueError("interactive must be a boolean")
        if not isinstance(self.argv, (list, tuple)) or len(self.argv) > 128:
            raise ValueError("argv must be a list or tuple of at most 128 strings")
        resolved_executable = absolute(self.executable)
        if (Path(resolved_executable).suffix.casefold() in {".bat", ".cmd"}
                and any(isinstance(arg, str) and any(c in arg for c in '&|<>^%!"\r\n\0')
                        for arg in self.argv)):
            raise BatchArgumentMetacharacterRejected("unsafe batch argument metacharacter")
        if any(not isinstance(a, str) or "\0" in a or len(a) > 8192 for a in self.argv):
            raise ValueError("invalid argv element")
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 0.01 <= self.timeout <= 3600:
            raise ValueError("timeout must be finite and between 0.01 and 3600 seconds")
        if type(self.output_limit) is not int or not 128 <= self.output_limit <= 65536:
            raise ValueError("output_limit must be an integer between 128 and 65536 bytes")
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 128:
            raise ValueError("invalid request ID")
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "executable", resolved_executable)
        object.__setattr__(self, "cwd", absolute(self.cwd))
