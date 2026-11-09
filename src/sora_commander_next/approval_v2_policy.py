"""Approval v2 launch-surface policy. Classification only; NEVER dispatches.

Permanent grants live in a separate Owner-controlled ledger. There is no
in-memory SCOPED grant, remote approval action or process-launch capability.
The policy checks the launch envelope only: it intentionally does not inspect
script contents, imports, libraries or child processes.
"""
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .commands import Command, absolute


class SurfaceDecision(StrEnum):
    AUTO = "AUTO"
    REVIEW = "REVIEW"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class AutoLaunch:
    """Exact Owner-configured, harmless version-query launch pattern."""
    executable: str
    argv: tuple[str, ...]

    def __post_init__(self):
        if type(self.executable) is not str or type(self.argv) is not tuple:
            raise ValueError("exact executable/argv tuple required")
        object.__setattr__(self, "executable", absolute(self.executable))
        if (len(self.argv) > 128
                or any(type(a) is not str or "\0" in a or len(a) > 8192
                       for a in self.argv)):
            raise ValueError("invalid exact version arguments")
        if self.argv not in (("--version",), ("-V",)):
            raise ValueError("AUTO may only contain a fixed version query")

    def matches(self, request):
        return (type(request) is Command
                and Path(self.executable) == Path(request.executable)
                and self.argv == request.argv)


@dataclass(frozen=True, slots=True, init=False)
class SurfacePolicyV2:
    """Immutable launch configuration, not an authorization or launch service."""

    roots: tuple[str, ...]
    auto: tuple[AutoLaunch, ...]

    def __init__(self, *, roots, auto=()):
        if type(roots) not in (tuple, list) or not roots:
            raise ValueError("explicit roots required")
        normalized_roots = tuple(absolute(value) for value in roots)
        if any(Path(root).parent == Path(root) for root in normalized_roots):
            raise ValueError("whole-disk roots forbidden")
        if (type(auto) not in (tuple, list)
                or any(type(item) is not AutoLaunch for item in auto)):
            raise ValueError("exact version AUTO patterns required")
        object.__setattr__(self, "roots", normalized_roots)
        object.__setattr__(self, "auto", tuple(auto))

    def classify(self, request):
        if type(request) is not Command:
            return SurfaceDecision.DENY
        if not any(
                Path(request.cwd).is_relative_to(Path(root)) for root in self.roots):
            return SurfaceDecision.DENY

        name = Path(request.executable).stem.lower()
        args = tuple(arg.lower() for arg in request.argv)
        if name in {"sudo", "su", "runas", "psexec"} or "runas" in args:
            return SurfaceDecision.DENY

        risky = name in {
            "powershell", "pwsh", "cmd", "sh", "bash", "env",
            "curl", "wget", "ssh", "scp",
        }
        risky |= Path(request.executable).suffix.lower() in {
            ".ps1", ".cmd", ".bat", ".py", ".sh", ".js",
        }
        risky |= any(any(character in arg for character in ";&|><=$\x60\n\r")
                     for arg in args)
        if (not risky and request.argv in (("--version",), ("-V",))
                and any(pattern.matches(request) for pattern in self.auto)):
            return SurfaceDecision.AUTO
        return SurfaceDecision.REVIEW
