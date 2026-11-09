"""Bounded, read-only AST closure audit of the protected Broker CORE candidate.

Only the explicit flat source package is inspected; scanned code is NEVER
imported or executed. Dynamic import capabilities are conservatively refused,
even with literal arguments. External stdlib/pywin32 imports are not resolved.
This is not publisher/hash/signature/ACL attestation, sys.path isolation, a
concurrent mutation guarantee, or a runtime security boundary. HOLD_LIVE.
"""
import ast
from dataclasses import dataclass, field
from pathlib import Path
import re
import stat
import sys

from .release_inventory import DEV_ONLY_MODULES, MODULES, PACKAGE


ROOTS = (
    "broker_owner_process.py",
    "broker_launch_fence.py",
    "broker_owner_lease.py",
)
MAX_FILES = 128
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_AST_NODES = 200_000
# Names only: this does not authenticate these dependencies or their versions.
_EXTERNAL_MODULES = frozenset((
    "win32api", "win32security", "win32con", "ntsecuritycon",
))
_DYNAMIC_NAMES = frozenset(("import_module", "__import__", "__builtins__", "eval", "exec"))
_DYNAMIC_MODULES = frozenset(("importlib", "builtins"))


class BrokerImportAuditError(ValueError):
    """Static evidence is unavailable or violates the reviewed closure."""


@dataclass(frozen=True, slots=True)
class BrokerImportEvidence:
    roots: tuple[str, ...]
    reachable_modules: tuple[str, ...]
    count: int
    authorization: bool = field(default=False, init=False)
    deployment_authorized: bool = field(default=False, init=False)
    authenticated: bool = field(default=False, init=False)
    status: str = field(default="HOLD_LIVE", init=False)


def _ordinary(path, *, directory):
    """Check every ancestor without resolve(), which would hide links."""
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except OSError as exc:
            raise BrokerImportAuditError("missing or unreadable source path") from exc
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise BrokerImportAuditError("symlink/reparse source path denied")
        want_directory = directory or part != path
        if not (stat.S_ISDIR(info.st_mode) if want_directory
                else stat.S_ISREG(info.st_mode)):
            raise BrokerImportAuditError("ordinary source directory/file required")
    return info


def _source_root(source_package):
    if not isinstance(source_package, (str, Path)):
        raise BrokerImportAuditError("explicit absolute source package required")
    raw = str(source_package)
    if (not raw or any(ord(c) < 32 for c in raw)
            or raw.startswith(("\\\\", "//", "\\??\\"))
            or any(p in (".", "..") or p.endswith((" ", "."))
                   for p in re.split(r"[\\/]", raw) if p)):
        raise BrokerImportAuditError("ambiguous source root denied")
    root = Path(raw)
    if (not root.is_absolute() or root.name != PACKAGE
            or ":" in raw[len(root.drive):]):
        raise BrokerImportAuditError("unknown source package root")
    # Never inspect conventional protected install/state trees, on any drive.
    # This is lexical path hygiene, not an ACL or drive provenance proof.
    protected = {"windows", "program files", "program files (x86)", "programdata"}
    if any(p.casefold() in protected for p in root.parts):
        raise BrokerImportAuditError("protected install/state root denied")
    _ordinary(root, directory=True)
    return root


def _classification():
    runtime = frozenset(MODULES)
    if (len(runtime) != len(MODULES) or runtime & DEV_ONLY_MODULES
            or not {"__init__.py", *ROOTS} <= runtime
            or any(type(n) is not str or re.fullmatch(
                r"(?:[a-z][a-z0-9_]*|__init__)\.py", n) is None
                for n in runtime | DEV_ONLY_MODULES)):
        raise BrokerImportAuditError("invalid reviewed module classification/roots")
    return runtime


def _dependencies(tree, runtime):
    dependencies = set()

    def local(module):
        # Inventory is deliberately flat. Unreviewed subpackages are denied.
        if module == PACKAGE:
            dependencies.add("__init__.py")
            return
        parts = module.split(".")
        if (len(parts) != 2 or parts[0] != PACKAGE
                or not parts[1].isidentifier()):
            raise BrokerImportAuditError("unclassified local module: " + module)
        name = parts[1] + ".py"
        if name in DEV_ONLY_MODULES:
            raise BrokerImportAuditError("DEV_ONLY dependency: " + module)
        if name not in runtime:
            raise BrokerImportAuditError("unclassified local module: " + module)
        dependencies.add(name)

    def absolute(module):
        top = module.split(".")[0]
        if top == PACKAGE:
            local(module)
        elif top in _DYNAMIC_MODULES:
            raise BrokerImportAuditError("dynamic import capability denied")
        elif top not in sys.stdlib_module_names and top not in _EXTERNAL_MODULES:
            # No implicit fallback to a sibling project or sys.path lookup.
            raise BrokerImportAuditError("unclassified absolute import: " + module)

    for node in ast.walk(tree):
        # Catch aliasing/references too, not just direct Call nodes. importlib
        # and builtins imports are refused so getattr aliases cannot hide them.
        if ((isinstance(node, ast.Name) and node.id in _DYNAMIC_NAMES)
                or (isinstance(node, ast.Attribute) and node.attr in _DYNAMIC_NAMES)):
            raise BrokerImportAuditError("dynamic import/eval/exec denied")
        if isinstance(node, ast.Import):
            for alias in node.names:
                absolute(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level != 1:
                    raise BrokerImportAuditError("relative import escapes package")
                module = PACKAGE + ("." + node.module if node.module else "")
            else:
                module = node.module or ""
            if module == PACKAGE:
                local(PACKAGE)
                for alias in node.names:
                    # Root-package attributes and star imports are ambiguous;
                    # require each such target to be an inventoried module.
                    local(PACKAGE + "." + alias.name)
            else:
                absolute(module)
                if any(alias.name in _DYNAMIC_NAMES for alias in node.names):
                    raise BrokerImportAuditError("dynamic import capability denied")
    return dependencies


def audit_broker_import_closure(source_package):
    """Inspect a fixed ROOTS closure under an explicit absolute package dir.

    No root override or bypass is provided. Both runtime modules and the package
    initializer are traversed, including imports nested under any AST branch.
    Unknown external names are refused; external dependency bytes, reflective
    arbitrary Python behavior and concurrent file changes remain out of scope.
    """
    root = _source_root(source_package)
    runtime = _classification()
    pending = set(ROOTS) | {"__init__.py"}
    visited = set()
    total_bytes = total_nodes = 0
    while pending:
        name = min(pending)
        pending.remove(name)
        if name in visited:
            continue
        if name not in runtime or name in DEV_ONLY_MODULES:
            raise BrokerImportAuditError("invalid closure root/dependency")
        if len(visited) >= MAX_FILES:
            raise BrokerImportAuditError("file count bound exceeded")
        path = root / name
        info = _ordinary(path, directory=False)
        # Python prefers a same-named subpackage over foo.py. The flat reviewed
        # inventory cannot describe it, so fail even when foo.py is also present.
        if name != "__init__.py":
            try:
                (root / path.stem).lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise BrokerImportAuditError("unreadable module shadow path") from exc
            else:
                raise BrokerImportAuditError("unreviewed subpackage/shadow path")
        if info.st_size > MAX_FILE_BYTES:
            raise BrokerImportAuditError("source file byte bound exceeded")
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise BrokerImportAuditError("source read failed") from exc
        total_bytes += len(data)
        if len(data) > MAX_FILE_BYTES or total_bytes > MAX_TOTAL_BYTES:
            raise BrokerImportAuditError("source byte bound exceeded")
        try:
            tree = ast.parse(data.decode("utf-8"), filename=name)
            total_nodes += sum(1 for _ in ast.walk(tree))
        except (UnicodeError, SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            raise BrokerImportAuditError("invalid UTF8/Python source: " + name) from exc
        if total_nodes > MAX_AST_NODES:
            raise BrokerImportAuditError("AST node bound exceeded")
        pending.update(_dependencies(tree, runtime) - visited)
        visited.add(name)
    names = tuple(sorted(PACKAGE if n == "__init__.py"
                         else PACKAGE + "." + n[:-3] for n in visited))
    return BrokerImportEvidence(ROOTS, names, len(names))
