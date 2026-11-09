"""DEV_ONLY immutable exact-file source proposal. No IO, GUI or writer.

This is a lexical preview filter ONLY. Never use an untrusted request to construct
this scope; a future protected Broker must supply and authenticate local grants.
No lexical check proves junction-free ancestors, ACLs, durable CAS, or Owner consent.
"""
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import PureWindowsPath
import re

MAX_GRANTS = 16
MAX_BYTES = 16384
_ALLOWED_EXTENSIONS = frozenset({
    ".py", ".pyi", ".md", ".json", ".toml", ".yaml", ".yml",
    ".ps1", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".sql", ".css", ".html", ".xml",
})
_FORBIDDEN = frozenset({
    ".git", ".venv", ".next", "node_modules", "build", "dist",
    "target", "__pycache__", "bin", "obj",
})
_DEVICE = re.compile(r"(?i)(?:con|prn|aux|nul|clock\$|com[0-9]|lpt[0-9])(?:\..*)?")
_SHA = re.compile(r"[0-9a-f]{64}")


class ScopeRejected(ValueError):
    """No scope or preview grants writing or implicit Owner approval."""


def _literal(path, *, file):
    if type(path) is not str or not 4 <= len(path) <= 2048:
        raise ScopeRejected("bounded literal path required")
    if not re.match(r"^[A-Z]:\\", path):
        raise ScopeRejected("exact canonical local drive path required")
    parsed = PureWindowsPath(path)
    if str(parsed) != path or not parsed.is_absolute() or len(parsed.parts) < 2:
        raise ScopeRejected("noncanonical path")
    for part in parsed.parts[1:]:
        if (part in (".", "..") or part.startswith(".") or part.endswith((" ", "."))
                or part.casefold() in _FORBIDDEN or "~" in part
                or _DEVICE.fullmatch(part) is not None
                or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in part)):
            raise ScopeRejected("ambiguous, private or managed path")
    if file and parsed.suffix.casefold() not in _ALLOWED_EXTENSIONS:
        raise ScopeRejected("unreviewed source extension")
    return parsed


@dataclass(frozen=True, slots=True)
class ExactSourceGrant:
    path: str
    operations: tuple[str, ...]

    def __post_init__(self):
        _literal(self.path, file=True)
        if (type(self.operations) is not tuple or not self.operations
                or len(self.operations) > 2
                or len(set(self.operations)) != len(self.operations)
                or any(type(op) is not str or op not in ("patch", "create")
                       for op in self.operations)):
            raise ScopeRejected("explicit patch/create grants required")


@dataclass(frozen=True, slots=True)
class SourceProposalOnly:
    path: str
    operation: str
    old_sha256: str | None
    new_sha256: str
    bytes: int
    changed: bool
    status: str = field(default="PREVIEW_ONLY", init=False)
    authority: str = field(default="NONE_DATA_ONLY", init=False)
    write_authorized: bool = field(default=False, init=False)
    owner_approved: bool = field(default=False, init=False)
    native_identity_verified: bool = field(default=False, init=False)


class OfflineExactSourceScope:
    """Exact local file grants fixed on construction. No remote grant API.

    A request is never allowed to add/replace grants. This class has no
    inspect/open, commit, replace, create or callback interface.
    """
    __slots__ = ("_root", "_grants")

    def __init__(self, root: str, grants: tuple[ExactSourceGrant, ...] = ()):
        _literal(root, file=False)
        if (type(grants) is not tuple or len(grants) > MAX_GRANTS
                or any(type(g) is not ExactSourceGrant for g in grants)):
            raise ScopeRejected("bounded locally configured exact grants required")
        distinct = set()
        for grant in grants:
            if not grant.path.startswith(root + "\\"):
                raise ScopeRejected("grant outside configured development root")
            name = grant.path.casefold()
            if name in distinct:
                raise ScopeRejected("duplicate Windows file identity")
            distinct.add(name)
        self._root = root
        self._grants = grants

    def preview(self, operation, path, before, after, expected_old_sha256=None):
        """Data-only SHA comparison. Caller bytes do not prove disk identity."""
        if type(operation) is not str or operation not in ("patch", "create"):
            raise ScopeRejected("explicit edit operation required")
        _literal(path, file=True)
        if not any(g.path == path and operation in g.operations for g in self._grants):
            raise ScopeRejected("not an exact locally configured grant")
        if type(after) is not bytes or len(after) > MAX_BYTES or b"\x00" in after:
            raise ScopeRejected("bounded UTF-8 proposal required")
        after.decode("utf-8")
        if operation == "create":
            if before is not None or expected_old_sha256 is not None:
                raise ScopeRejected("create requires proven absence; collision")
            old_hash = None
        else:
            if (type(before) is not bytes or len(before) > MAX_BYTES or b"\x00" in before
                    or type(expected_old_sha256) is not str
                    or _SHA.fullmatch(expected_old_sha256) is None):
                raise ScopeRejected("exact bounded prior bytes and digest required")
            before.decode("utf-8")
            old_hash = sha256(before).hexdigest()
            if expected_old_sha256 != old_hash:
                raise ScopeRejected("stale source proposal")
        return SourceProposalOnly(path, operation, old_hash, sha256(after).hexdigest(),
                                  len(after), before != after)
