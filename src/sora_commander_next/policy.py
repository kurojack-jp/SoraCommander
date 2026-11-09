"""Local configuration only: exact patterns, explicit roots, in-memory grants."""

from dataclasses import dataclass
from enum import StrEnum
from functools import wraps
import math
from pathlib import Path
from time import monotonic
from threading import RLock
from uuid import uuid4

from .commands import Command, absolute
from .git_push_guard import push_refused


class Decision(StrEnum):
    AUTO = "AUTO"
    SCOPED = "SCOPED"
    REVIEW = "REVIEW"
    DENY = "DENY"


def locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


def within(cwd: str, root: str) -> bool:
    return Path(cwd).is_relative_to(Path(root))


@dataclass(frozen=True)
class Pattern:
    executable: str
    argv: tuple[str, ...]

    def __post_init__(self):
        validated = Command(self.executable, self.argv, str(Path.cwd()))
        object.__setattr__(self, "executable", validated.executable)
        object.__setattr__(self, "argv", validated.argv)

    def matches(self, request: Command) -> bool:
        return Path(self.executable) == Path(request.executable) and self.argv == request.argv


@dataclass(frozen=True)
class Grant:
    grant_id: str
    owner: str
    root: str
    patterns: tuple[Pattern, ...]
    expires_at: float


class Policy:
    def __init__(self, roots=(), auto=(), clock=monotonic, *, read_only_roots=()):
        self.lock = RLock()
        self.instance_id = uuid4().hex
        self.roots = tuple(absolute(root) for root in roots)
        self.read_only_roots = tuple(absolute(root) for root in read_only_roots)
        if any(Path(root).parent == Path(root) for root in self.read_roots):
            raise ValueError("whole-disk roots are forbidden")
        if any(within(read, full) or within(full, read)
               for read in self.read_only_roots for full in self.roots):
            raise ValueError("full and read-only roots must not overlap")
        self.auto, self.clock, self.generation = tuple(auto), clock, 0
        self._grants: dict[str, Grant] = {}
        self._grant_guards = {}  # optional Owner-session checks, local only

    @property
    def read_roots(self):
        return self.roots + self.read_only_roots

    def require_full_path(self, path):
        """Narrow legacy pilots/forwarders too; this is no write authorization."""
        if (type(path) is not str or not path or "\0" in path
                or not Path(path).is_absolute()
                or not any(within(path, root) for root in self.roots)):
            raise PermissionError("full-access folder required")
        resolved = absolute(path)
        if not any(within(resolved, root) for root in self.roots):
            raise PermissionError("full-access folder required")

    @locked
    def add_grant_local(self, grant: Grant):
        root = absolute(grant.root)
        if not grant.grant_id or not grant.owner or not grant.patterns or not all(isinstance(p, Pattern) for p in grant.patterns):
            raise ValueError("explicit Owner, ID and exact patterns required")
        if type(grant.expires_at) not in (int, float) or not math.isfinite(grant.expires_at) or grant.expires_at <= self.clock():
            raise ValueError("grant must have a finite future expiry")
        if not any(within(root, known) for known in self.roots):
            raise ValueError("grant root must be inside a configured root")
        if grant.grant_id in self._grants:
            raise ValueError("grant ID already exists")
        self._grants[grant.grant_id] = Grant(grant.grant_id, grant.owner, root, tuple(grant.patterns), grant.expires_at)
        self.generation += 1

    @locked
    def bind_grant_guard_local(self, grant_id: str, owner: str, guard):
        if self._grants[grant_id].owner != owner or not callable(guard):
            raise PermissionError("grant guard cannot be attached")
        guard()
        self._grant_guards[grant_id] = guard
        self.generation += 1

    @locked
    def revoke_local(self, grant_id: str, owner: str):
        if self._grants[grant_id].owner != owner:
            raise PermissionError("only the granting Owner can revoke this grant")
        del self._grants[grant_id]
        self._grant_guards.pop(grant_id, None)
        self.generation += 1

    @locked
    def revoke_owner_grants_local(self, owner: str):
        ids = [key for key, grant in self._grants.items() if grant.owner == owner]
        for key in ids:
            del self._grants[key]
            self._grant_guards.pop(key, None)
        self.generation += 1
        return len(ids)

    @locked
    def classify(self, request: Command, owner: str) -> Decision:
        if not any(within(request.cwd, root) for root in self.roots):
            return Decision.DENY
        name = Path(request.executable).stem.lower()
        args = tuple(a.lower() for a in request.argv)
        if name in {"sudo", "su", "runas", "psexec"} or "runas" in args:
            return Decision.DENY
        if push_refused(request.executable, request.argv):
            return Decision.DENY
        for grant_id, g in tuple(self._grants.items()):
            if g.owner == owner and self.clock() < g.expires_at and within(request.cwd, g.root) and any(p.matches(request) for p in g.patterns):
                guard = self._grant_guards.get(grant_id)
                try:
                    if guard is not None:
                        guard()
                except Exception:
                    self._grants.pop(grant_id, None)
                    self._grant_guards.pop(grant_id, None)
                    self.generation += 1
                    continue
                return Decision.SCOPED
        # Deliberately conservative: only fixed version queries may become AUTO.
        risky = name in {"powershell", "pwsh", "cmd", "sh", "bash", "env", "curl", "wget", "ssh", "scp"}
        risky |= Path(request.executable).suffix.lower() in {".ps1", ".cmd", ".bat", ".py", ".sh", ".js"}
        risky |= any(any(c in a for c in ";&|><=$`\n\r") for a in args)
        if not risky and request.argv in (("--version",), ("-V",)) and any(p.matches(request) for p in self.auto):
            return Decision.AUTO
        return Decision.REVIEW
