"""Phase10C: bounded local read-only file helpers, no shell execution."""
from collections import deque
from pathlib import Path
from hashlib import sha256
import os
import stat

class ReadBoundaryError(ValueError):
    pass


class ReadRootScopeError(ReadBoundaryError):
    def __init__(self, path, message):
        super().__init__(message)
        self.path = path


def _require_local_path(value, error_type):
    if str(value).startswith(("\\\\", "//")) or Path(value).drive.startswith("\\\\"):
        raise error_type("UNC and device paths are unsupported")


def _drive_type(anchor):
    if os.name == "nt":
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(anchor))
    return None


_NAME_SURROGATE = 0x20000000  # Microsoft reparse-tag bit: "this stands for another name"
_HIDDEN_OR_SYSTEM = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_RECALL_ATTRIBUTES = (FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
                      | stat.FILE_ATTRIBUTE_OFFLINE)


def _is_redirecting_link(info, error_type):
    """True for a symlink or junction; data-only reparse points are plain entries.

    OneDrive cloud files and deduplicated files carry FILE_ATTRIBUTE_REPARSE_POINT
    but do not redirect the path, and os.readlink cannot read them. Any other
    name-surrogate tag would redirect in a way this check cannot inspect.
    """
    if stat.S_ISLNK(info.st_mode):
        return True
    if not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    tag = getattr(info, "st_reparse_tag", 0)
    if tag in (stat.IO_REPARSE_TAG_SYMLINK, stat.IO_REPARSE_TAG_MOUNT_POINT):
        return True
    if tag & _NAME_SURROGATE:
        raise error_type("link target could not be inspected")
    return False


def _visible_entry(info):
    if (getattr(info, "st_file_attributes", 0) & _HIDDEN_OR_SYSTEM
            or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
        return False
    try:
        return not _is_redirecting_link(info, ReadBoundaryError)
    except ReadBoundaryError:
        # Unknown name-surrogate tags are also excluded from enumeration.
        return False


def _recall_required(info):
    return bool(getattr(info, "st_file_attributes", 0) & _RECALL_ATTRIBUTES)


def _require_no_network_link(path, error_type):
    """Inspect each link before traversing it or calling resolve."""
    if os.name == "nt":
        # Win32 normalizes the supplied DOS path before opening it.
        path = Path(os.path.normpath(path))
    anchor, *parts = path.parts
    current = Path(anchor)
    pending = deque(parts)
    hops = 0
    while pending:
        part = pending.popleft()
        if part == "..":
            current = current.parent
            continue
        component = current / part
        try:
            info = component.lstat()
        except (FileNotFoundError, NotADirectoryError):
            if ".." in pending:
                raise error_type("path component could not be inspected") from None
            return
        except OSError:
            raise error_type("path component could not be inspected") from None
        if not _is_redirecting_link(info, error_type):
            current = component
            continue

        hops += 1
        if hops > 40:
            raise error_type("too many path link hops")
        try:
            target = os.readlink(component)
        except OSError:
            raise error_type("link target could not be inspected") from None

        # Junctions normally return an extended-length local DOS path.
        spelling = target.replace("/", "\\")
        extended_drive = (
            spelling.startswith("\\\\?\\") and len(spelling) >= 7
            and spelling[4] in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            and spelling[5:7] == ":\\"
        )
        if spelling.startswith("\\\\") and not extended_drive:
            raise error_type("link leads to a network location")
        target_path = Path(target)
        if target_path.drive and not target_path.root:
            raise error_type("link target could not be inspected")
        destination = current / target_path
        if destination.drive:
            drive_root = destination.drive.removeprefix("\\\\?\\") + "\\"
            if _drive_type(drive_root) == 4:  # DRIVE_REMOTE
                raise error_type("link leads to a network location")
        current = Path(destination.anchor)
        pending.extendleft(reversed(destination.parts[1:]))


# Deny secret-bearing paths regardless of an Owner-selected read root. This is
# defense in depth, NOT a substitute for scoped roots, OS ACLs, or provenance.
# Avoid blanket JSON/TOML/LOG bans: ordinary source review and diagnostics
# rely on those formats. Instead deny credential families and private stores.
_READ_DENY_COMPONENTS = frozenset({
    "secrets", "secret", "credentials", "credential", "tokens", "token",
    "private_keys", "private-keys", "private_key", "private-key",
    "node_modules", "__pycache__", "venv",
    "auth.json", "oauth.json", "api-keys.json", "apikeys.json",
    "id_rsa", "id_ed25519", "id_dsa", "id_ecdsa",
    "owner-output-archive.json", "owner-command-journal.json",
    "owner-journal.json", "owner-journal.jsonl",
    "start-sora-commander.cmd", "state", "runtime", "logs", "temp",
})
# Key/credential formats are denied for any path component, including a root
# and its ancestors. Data/archive formats are denied only below a root: as a
# root or ancestor directory name (project.db, snapshot.bak) they are ordinary.
_READ_DENY_SECRET_SUFFIXES = (
    ".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".kdbx",
)
_READ_DENY_SUFFIXES = _READ_DENY_SECRET_SUFFIXES + (
    ".env",
    ".sqlite", ".sqlite3", ".db", ".db3", ".wal", ".shm", ".journal",
    ".zip", ".7z", ".rar", ".tar", ".tgz", ".gz", ".bak", ".backup",
)
_READ_DENY_PREFIXES = (
    "credentials.", "credential.", "secret.", "secrets.",
    "token.", "tokens.", "api_key.", "api-key.", "private_key.",
    "private-key.", "owner-output-archive.", "owner-command-journal.",
    "owner-journal.",
)
_READ_DEVICE_NAMES = frozenset({
    "con", "prn", "aux", "nul", "clock$", "conin$", "conout$",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})


def _read_name_denied(name):
    """Suppress protected names from direct reads and directory discovery."""
    if type(name) is not str or not name or name.startswith("."):
        return True
    folded = name.casefold()
    if (name.endswith((" ", ".")) or any(ord(char) < 32 for char in name)
            or ":" in name or folded.split(".", 1)[0] in _READ_DEVICE_NAMES):
        return True
    return (folded.endswith(".user.txt")
            or folded in _READ_DENY_COMPONENTS
            or folded.startswith(_READ_DENY_PREFIXES)
            or folded.endswith(_READ_DENY_SUFFIXES))


def _sensitive_component(name):
    """Protected-name policy for a root and its ancestors.

    Same name and prefix rules as _read_name_denied, but only its key and
    credential suffixes, and without its hidden/device spelling rules: those
    and the data/archive suffixes apply only below a configured root.
    """
    folded = name.casefold()
    return (folded.endswith(".user.txt")
            or folded in _READ_DENY_COMPONENTS
            or folded.startswith(_READ_DENY_PREFIXES)
            or folded.endswith(_READ_DENY_SECRET_SUFFIXES))


def _check(path,roots):
    _require_local_path(path, ReadBoundaryError)
    p=Path(path)
    if not p.is_absolute() or (roots and any(type(r) is not str for r in roots)):
        raise ReadBoundaryError("absolute path and configured roots required")
    if not roots:
        raise ReadRootScopeError(path, "absolute path and configured roots required")
    # Reject traversal and NTFS alternate data streams before canonicalization.
    # The drive anchor may contain a colon; child components never may.
    if any(part == ".." or ":" in part for part in p.parts[1:]):
        raise ReadBoundaryError("ambiguous read path")
    # Check the supplied spelling before canonicalization, including when a
    # sensitive directory was configured as the root itself.
    if any(_sensitive_component(part) for part in p.parts[1:]):
        raise ReadBoundaryError("sensitive path excluded from read scope")
    _require_no_network_link(p, ReadBoundaryError)
    p=p.resolve(strict=True)
    allowed=False
    for r in roots:
        root=Path(r)
        if not root.is_absolute():
            raise ReadBoundaryError("absolute root required")
        _require_local_path(root, ReadBoundaryError)
        _require_no_network_link(root, ReadBoundaryError)
        resolved=root.resolve(strict=True)
        if p.is_relative_to(resolved):
            # A bad root selection cannot grant access to a secret vault.
            # Check its ancestor names as well, without rejecting ordinary
            # hidden temporary fixtures or the Owner's normal workspace.
            if (any(_sensitive_component(part) or
                    part.casefold() in (".git", ".venv", ".ssh", ".aws")
                    for part in resolved.parts[1:]) or
                    (resolved.is_file() and _read_name_denied(resolved.name))):
                raise ReadBoundaryError("sensitive read root excluded")
            # Restrict only path components *below* the configured root: a
            # legitimate Owner-selected root can itself be a hidden temp dir.
            if any(_read_name_denied(part) for part in p.relative_to(resolved).parts):
                raise ReadBoundaryError("sensitive path excluded from read scope")
            allowed=True
    if not allowed:
        raise ReadRootScopeError(path, "outside authorized roots")
    for parent in (Path(path),*Path(path).parents):
        if parent.is_symlink() or (hasattr(parent,"is_junction") and parent.is_junction()):
            raise ReadBoundaryError("linked path")
    return p

def list_folder(path,roots,limit=100,depth=0):
    if type(limit) is not int or not 1<=limit<=200:
        raise ReadBoundaryError("invalid limit")
    if type(depth) is not int or not 0<=depth<=4:
        raise ReadBoundaryError("invalid listing depth")
    if depth:
        visible = []
        visit_counts = []
        # Reuse the navigation scanner's total-entry budget and name policy.
        # Fully consume the bounded scan so truncation never bypasses its cap.
        for entry, relative, kind in _navigation_entries(
                path, roots, depth, report_visits=visit_counts.append):
            visible.append({"name": entry.name, "type": kind, "path": relative})
        return {"entries": visible[:limit], "truncated": len(visible)>limit,
                "omitted_entries": visit_counts[0] - len(visible)}
    p=_check(path,roots)
    if not p.is_dir():
        raise ReadBoundaryError("not a folder")
    visible, omitted_entries = _folder_entries(p)
    visible.sort(key=lambda item:item[0].name.lower())
    return {"entries":[{"name":q.name,"type":"directory" if is_dir else "file"}
                       for q, is_dir in visible[:limit]],
            "truncated":len(visible)>limit,
            "omitted_entries": omitted_entries}

def read_text_file(path,roots,max_bytes=65536):
    import os
    if type(max_bytes) is not int or not 1<=max_bytes<=65536:
        raise ReadBoundaryError("invalid byte limit")
    p=_check(path,roots)
    # The previous body read without a link or identity check, so a
    # hardlinked file (or a file swapped mid-read) was accepted. Mirror the
    # hardened inspection readers: single-link pre-check, open-file
    # revalidation before and after the read, and named-path revalidation.
    # Existing messages and the return shape are preserved; only the new
    # rejections add messages.
    meta=p.stat()
    if not stat.S_ISREG(meta.st_mode):
        raise ReadBoundaryError("not an ordinary file")
    if meta.st_nlink != 1:
        raise ReadBoundaryError("hardlinked file rejected")
    if meta.st_size>max_bytes:
        raise ReadBoundaryError("file exceeds limit")
    with p.open("rb") as stream:
        before=os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size>max_bytes
                or (before.st_dev, before.st_ino) != (meta.st_dev, meta.st_ino)):
            raise ReadBoundaryError("file changed during read")
        data=stream.read(max_bytes+1)
        after=os.fstat(stream.fileno())
    if (len(data)>max_bytes or len(data)!=after.st_size or after.st_nlink != 1
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ReadBoundaryError("file changed during read")
    _verify_named_file(path, roots, after)
    try:
        text=data.decode("utf-8")
    except UnicodeError as exc:
        raise ReadBoundaryError("UTF-8 text required") from exc
    return {"content":text,"bytes":len(data),"sha256":sha256(data).hexdigest()}

MAX_PAGED_FILE_BYTES = 4 * 1024 * 1024


def _verify_named_file(path, roots, opened):
    """Best-effort revalidation, not an atomic filesystem security boundary."""
    named = _check(path, roots).stat()
    if (not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
            or (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)):
        raise ReadBoundaryError("file identity changed")


def _read_inspection_text(path, roots, max_bytes, *, skip_recall=False):
    """Bounded single-link read for batch inspection and comparison."""
    import os
    p = _check(path, roots)
    meta = p.stat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > max_bytes:
        raise ReadBoundaryError("unsupported inspection file")
    if skip_recall and _bulk_file_may_recall(p):
        return None
    with p.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > max_bytes
                or (before.st_dev, before.st_ino) != (meta.st_dev, meta.st_ino)):
            raise ReadBoundaryError("inspection file changed")
        raw = stream.read(max_bytes + 1)
        after = os.fstat(stream.fileno())
    if (len(raw) > max_bytes or len(raw) != after.st_size or after.st_nlink != 1
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ReadBoundaryError("inspection file changed")
    _verify_named_file(path, roots, after)
    try:
        content = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ReadBoundaryError("UTF-8 text required") from exc
    return {"content": content, "bytes": len(raw), "sha256": sha256(raw).hexdigest()}


def read_text_page(path, roots, *, offset=0, max_bytes=16384):
    """Read a bounded UTF-8 page and whole-file digest for large source files.

    Read-only. Caller-supplied offsets never widen the configured root.
    A 4MiB cap prevents an unbounded hash/read via remote inputs.
    """
    import os
    if (type(offset) is not int or not 0 <= offset <= MAX_PAGED_FILE_BYTES
            or type(max_bytes) is not int or not 1 <= max_bytes <= 65536):
        raise ReadBoundaryError("invalid page bounds")
    p = _check(path, roots)
    if not p.is_file():
        raise ReadBoundaryError("not an ordinary file")
    with p.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > MAX_PAGED_FILE_BYTES):
            raise ReadBoundaryError("unsupported paged file")
        raw = stream.read(MAX_PAGED_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (len(raw) > MAX_PAGED_FILE_BYTES or len(raw) != after.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ReadBoundaryError("file changed during read")
    # Check that the named path still designates the same ordinary file and
    # remains in the allowed tree after reading (not an OS-level atomic guard).
    checked = _check(path, roots)
    named = checked.stat()
    if (not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
            or (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ReadBoundaryError("file identity changed")
    try:
        raw.decode("utf-8")
    except UnicodeError as exc:
        raise ReadBoundaryError("UTF-8 text required") from exc
    if offset > len(raw):
        raise ReadBoundaryError("page offset exceeds file")
    end = min(offset + max_bytes, len(raw))
    # Keep each page on a UTF-8 code-point boundary, without corrupting text.
    while end < len(raw) and end > offset and raw[end] & 0xC0 == 0x80:
        end -= 1
    if end == offset and offset < len(raw):
        raise ReadBoundaryError("page too short for UTF-8 character")
    try:
        content = raw[offset:end].decode("utf-8")
    except UnicodeError as exc:
        raise ReadBoundaryError("page offset splits UTF-8 character") from exc
    return {
        "content": content,
        "offset": offset,
        "next_offset": end,
        "total_bytes": len(raw),
        "sha256": sha256(raw).hexdigest(),
        "truncated": end < len(raw),
    }

MAX_PAGED_FOLDER_ITEMS = 10000


def _folder_entries(folder):
    # Excluded entries still consume the enumeration budget.
    visible = []
    visited = 0
    for visited, entry in enumerate(folder.iterdir(), 1):
        if visited > MAX_PAGED_FOLDER_ITEMS:
            raise ReadBoundaryError("folder exceeds bounded listing")
        if (_read_name_denied(entry.name) or entry.is_symlink()
                or (hasattr(entry, "is_junction") and entry.is_junction())):
            continue
        info = entry.lstat()
        if _visible_entry(info):
            visible.append((entry, stat.S_ISDIR(info.st_mode)))
    return visible, visited - len(visible)


def list_folder_page(path, roots, *, offset=0, limit=100):
    """List a bounded page of non-linked children within an existing read root.

    Pagination is positional, not a stable snapshot when a directory mutates.
    Consumers should restart listing if concurrent edits may have occurred.
    """
    if (type(offset) is not int or not 0 <= offset <= MAX_PAGED_FOLDER_ITEMS
            or type(limit) is not int or not 1 <= limit <= 200):
        raise ReadBoundaryError("invalid folder page bounds")
    p = _check(path, roots)
    if not p.is_dir():
        raise ReadBoundaryError("not a folder")
    visible, omitted_entries = _folder_entries(p)
    if _check(path, roots) != p:
        raise ReadBoundaryError("folder path changed")
    visible.sort(key=lambda item: (item[0].name.casefold(), item[0].name))
    if offset > len(visible):
        raise ReadBoundaryError("folder page offset exceeds entries")
    selected = visible[offset:offset + limit]
    next_offset = offset + len(selected)
    return {
        "entries": [{"name": entry.name,
                     "type": "directory" if is_dir else "file"}
                    for entry, is_dir in selected],
        "offset": offset,
        "next_offset": next_offset,
        "total_entries": len(visible),
        "truncated": next_offset < len(visible),
        "omitted_entries": omitted_entries,
    }

# Phase 10: bounded, read-only source navigation for Owner-selected roots.
# Directory trees can mutate while being searched. These tools never grant
# access, write files, or claim a stable snapshot or process authentication.
_SKIP_NAV_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".next", "dist", "build", "target", "temp", "artifacts",
})
_SOURCE_SUFFIXES = frozenset({
    ".py", ".pyi", ".md", ".txt", ".toml", ".json", ".jsonl", ".yaml", ".yml",
    ".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".rs", ".ps1", ".psm1",
    ".sh", ".bat", ".cmd", ".vbs", ".ini", ".cfg", ".xml", ".sql", ".go",
    ".c", ".h", ".cpp", ".hpp", ".cs",
})
_MAX_NAV_VISITS = 8000
_MAX_SEARCH_BYTES = 12 * 1024 * 1024
_MAX_SEARCH_FILE = 1024 * 1024


def _navigation_entries(path, roots, depth, *, skip_recall=False, report_visits=None):
    """Yield visible entries and optionally report visits after a full scan."""
    from collections import deque
    if type(depth) is not int or not 0 <= depth <= 12:
        raise ReadBoundaryError("invalid navigation depth")
    root = _check(path, roots)
    if not root.is_dir():
        raise ReadBoundaryError("navigation requires a folder")
    pending = deque([(root, 0)])
    visited = 0
    while pending:
        directory, level = pending.popleft()
        # A junction swap during traversal must not broaden the selected root.
        try:
            checked_dir = _check(str(directory), roots)
        except PermissionError:
            if directory == root:
                raise
            continue
        if checked_dir != directory:
            raise ReadBoundaryError("navigation directory changed")
        entries = []
        try:
            with os.scandir(directory) as children:
                for child in children:
                    visited += 1
                    if visited > _MAX_NAV_VISITS:
                        raise ReadBoundaryError("navigation entry budget exceeded")
                    if (_read_name_denied(child.name)
                            or child.name.casefold() in _SKIP_NAV_DIRS):
                        continue
                    # Windows exposes RECALL_ON_OPEN in enumeration metadata.
                    # Check it before any Path operation that can open the entry.
                    if skip_recall and os.name == "nt" and _recall_required(
                            child.stat(follow_symlinks=False)):
                        continue
                    entries.append(Path(child.path))
        except PermissionError:
            # A denied child is never traversed; a denied starting root fails.
            if directory == root:
                raise
            continue
        entries.sort(key=lambda p: (p.name.casefold(), p.name))
        for entry in entries:
            if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
                continue
            try:
                meta = entry.lstat()
            except PermissionError:
                continue
            if not _visible_entry(meta) or (skip_recall and _recall_required(meta)):
                continue
            try:
                checked = _check(str(entry), roots)
            except PermissionError:
                continue
            if checked != entry:
                raise ReadBoundaryError("navigation path changed")
            is_dir = stat.S_ISDIR(meta.st_mode)
            if not (is_dir or stat.S_ISREG(meta.st_mode)):
                continue
            rel = str(entry.relative_to(root)).replace("\\", "/")
            yield entry, rel, "directory" if is_dir else "file"
            if is_dir and level < depth and entry.name.casefold() not in _SKIP_NAV_DIRS:
                pending.append((entry, level + 1))
    if report_visits is not None:
        report_visits(visited)


def find_files(path, roots, *, name, max_results=50, depth=8):
    """Find file/directory names in configured roots; case-insensitive literal."""
    if (type(name) is not str or not 1 <= len(name) <= 120
            or "\0" in name or type(max_results) is not int
            or not 1 <= max_results <= 100):
        raise ReadBoundaryError("invalid filename query")
    matches = []
    for _entry, relative, kind in _navigation_entries(path, roots, depth):
        if name.casefold() in relative.casefold():
            if len(matches) == max_results:
                return {"matches": matches, "count": len(matches), "truncated": True}
            matches.append({"path": relative, "type": kind})
    return {"matches": matches, "count": len(matches), "truncated": False}


def search_code(path, roots, *, text, max_results=50, depth=8):
    """Search bounded UTF-8 source code with line numbers; never run code."""
    import os
    if (type(text) is not str or not 1 <= len(text) <= 120
            or any(ch in text for ch in ("\0", "\r", "\n"))
            or type(max_results) is not int or not 1 <= max_results <= 100):
        raise ReadBoundaryError("invalid source search query")
    needle = text.casefold()
    found = []
    scanned_files = 0
    scanned_bytes = 0
    skipped_large = 0
    for entry, relative, kind in _navigation_entries(path, roots, depth, skip_recall=True):
        if kind != "file" or entry.suffix.casefold() not in _SOURCE_SUFFIXES:
            continue
        meta = entry.lstat()
        if _recall_required(meta):
            continue
        if meta.st_size > _MAX_SEARCH_FILE:
            skipped_large += 1
            continue
        if scanned_bytes + meta.st_size > _MAX_SEARCH_BYTES:
            return {"matches": found, "count": len(found), "truncated": True,
                    "scanned_files": scanned_files, "skipped_large": skipped_large}
        # Validate each concrete file again just before reading. No symlinks,
        # hard links or bytes beyond the 1MiB cap are eligible for code search.
        target = _check(str(entry), roots)
        if _bulk_file_may_recall(target):
            continue
        with target.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_size > _MAX_SEARCH_FILE):
                raise ReadBoundaryError("invalid searched file")
            remaining = _MAX_SEARCH_BYTES - scanned_bytes
            if before.st_size > remaining:
                return {"matches": found, "count": len(found), "truncated": True,
                        "scanned_files": scanned_files, "skipped_large": skipped_large}
            raw = stream.read(min(_MAX_SEARCH_FILE, remaining) + 1)
            after = os.fstat(stream.fileno())
        if (len(raw) > _MAX_SEARCH_FILE or len(raw) != after.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ReadBoundaryError("file changed during search")
        _verify_named_file(str(entry), roots, after)
        if len(raw) > remaining:
            raise ReadBoundaryError("search byte budget exceeded")
        scanned_bytes += len(raw)
        scanned_files += 1
        try:
            decoded = raw.decode("utf-8")
        except UnicodeError:
            continue
        for number, line in enumerate(decoded.splitlines(), 1):
            at = line.casefold().find(needle)
            if at < 0:
                continue
            # Bounded context around first match; never return whole raw logs.
            # casefold can expand a character (e.g. sharp s -> ss).
            # Translate the folded match span back to original indices.
            folded_pos = 0
            start = end = None
            for index, char in enumerate(line):
                folded_pos += len(char.casefold())
                if start is None and folded_pos > at:
                    start = index
                if folded_pos >= at + len(needle):
                    end = index + 1
                    break
            excerpt = line[max(0, start - 75): end + 115]
            if len(found) == max_results:
                return {"matches": found, "count": len(found),
                        "truncated": True, "scanned_files": scanned_files,
                        "skipped_large": skipped_large}
            found.append({"path": relative, "line": number,
                          "excerpt": excerpt[:300]})
    return {"matches": found, "count": len(found), "truncated": False,
            "scanned_files": scanned_files, "skipped_large": skipped_large}


def path_info(path, roots):
    """Small, read-only metadata and bounded whole-file hash for CAS preview."""
    import os
    p = _check(path, roots)
    before = p.stat()
    if stat.S_ISDIR(before.st_mode):
        return {"type": "directory", "size_bytes": None,
                "modified_ns": before.st_mtime_ns, "sha256": None}
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ReadBoundaryError("unsupported file metadata")
    digest = None
    if before.st_size <= MAX_PAGED_FILE_BYTES:
        with p.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if ((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                    != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)):
                raise ReadBoundaryError("metadata file changed")
            raw = stream.read(MAX_PAGED_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(raw) != after.st_size or
                (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ReadBoundaryError("metadata file changed")
        _verify_named_file(path, roots, after)
        digest = sha256(raw).hexdigest()
    _verify_named_file(path, roots, before)
    return {"type": "file", "size_bytes": before.st_size,
            "modified_ns": before.st_mtime_ns, "sha256": digest}

def read_text_lines(path, roots, *, start_line=1, max_lines=50):
    """Return bounded numbered lines, useful after source search results.

    This is not a stable snapshot across subsequent requests. The SHA-256
    identifies the exact whole-file bytes read for this response only.
    """
    import os
    if (type(start_line) is not int or not 1 <= start_line <= 1000000
            or type(max_lines) is not int or not 1 <= max_lines <= 100):
        raise ReadBoundaryError("invalid line page")
    p = _check(path, roots)
    if not p.is_file():
        raise ReadBoundaryError("not a regular text file")
    with p.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > MAX_PAGED_FILE_BYTES):
            raise ReadBoundaryError("line source exceeds bounded file")
        raw = stream.read(MAX_PAGED_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (len(raw) > MAX_PAGED_FILE_BYTES or len(raw) != after.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ReadBoundaryError("file changed during line read")
    checked = _check(path, roots)
    named = checked.stat()
    if (not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
            or (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ReadBoundaryError("line file identity changed")
    try:
        lines = raw.decode("utf-8").splitlines(keepends=True)
    except UnicodeError as exc:
        raise ReadBoundaryError("UTF-8 text required") from exc
    if start_line > len(lines) + 1:
        raise ReadBoundaryError("line page starts beyond EOF")
    result = []
    budget = 16384
    for i in range(start_line - 1, min(start_line - 1 + max_lines, len(lines))):
        line = lines[i]
        line_bytes = len(line.encode("utf-8"))
        if line_bytes > budget:
            if not result:
                raise ReadBoundaryError("line exceeds result budget; use byte paging")
            break
        result.append({"line": i + 1, "text": line})
        budget -= line_bytes
    next_line = start_line + len(result)
    return {"lines": result, "start_line": start_line, "next_line": next_line,
            "total_lines": len(lines), "sha256": sha256(raw).hexdigest(),
            "truncated": next_line <= len(lines)}

# Convenience operations for reviewing multiple existing files without a shell.
# These are READ-ONLY: neither function authorizes editing or creation.
def _bulk_file_may_recall(path):
    if os.name == "nt":
        # Use an exact-name directory query. lstat alone cannot report
        # RECALL_ON_OPEN on all providers and can open data-only reparse points.
        import ctypes
        from ctypes import wintypes

        class FindData(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("creation", wintypes.FILETIME),
                ("access", wintypes.FILETIME),
                ("write", wintypes.FILETIME),
                ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD),
                ("reserved0", wintypes.DWORD),
                ("reserved1", wintypes.DWORD),
                ("name", wintypes.WCHAR * 260),
                ("short_name", wintypes.WCHAR * 14),
            ]

        if any(ch in path.name for ch in ("*", "?", ":")):
            raise ReadBoundaryError("invalid bulk file name")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        find = kernel.FindFirstFileW
        find.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(FindData)]
        find.restype = wintypes.HANDLE
        close = kernel.FindClose
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        data = FindData()
        handle = find("\\\\?\\" + str(path), ctypes.byref(data))
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        if not close(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        if _read_name_denied(data.name):
            raise ReadBoundaryError("sensitive path excluded from read scope")
        if data.attributes & _RECALL_ATTRIBUTES and not data.attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
            if data.reserved0 & _NAME_SURROGATE:
                raise ReadBoundaryError("linked path")
            return True
    info = path.lstat()
    if _is_redirecting_link(info, ReadBoundaryError):
        raise ReadBoundaryError("linked path")
    return stat.S_ISREG(info.st_mode) and _recall_required(info)


def _skip_bulk_file(name, roots):
    _require_local_path(name, ReadBoundaryError)
    path = Path(name)
    if not path.is_absolute() or _read_name_denied(path.name):
        raise ReadBoundaryError("invalid bulk file path")
    # Check scope and reject parent links before querying child metadata.
    parent_name = str(path.parent)
    # The read boundary also supports a root that is the exact file itself.
    scope = (parent_name,) if any(type(root) is str and Path(root) == path
                                  for root in (roots or ())) else roots
    parent = _check(parent_name, scope)
    return _bulk_file_may_recall(parent / path.name)


def read_many_text_files(paths, roots, *, max_bytes_per_file=8192):
    """One bounded MCP request for at most eight Owner-scoped UTF-8 files."""
    if (type(paths) is not list or not 1 <= len(paths) <= 8
            or any(type(p) is not str or not p for p in paths)
            or len(set(paths)) != len(paths)
            or type(max_bytes_per_file) is not int
            or not 1 <= max_bytes_per_file <= 8192):
        raise ReadBoundaryError("invalid batch read request")
    # No partial successful response if a path falls outside configured roots.
    result = []
    for name in paths:
        # Like an out-of-scope path, a cloud-only file fails the whole request:
        # never drop it silently, and never download it in bulk.
        if _skip_bulk_file(name, roots):
            raise ReadBoundaryError("file requires recall; it is not stored locally")
        data = _read_inspection_text(name, roots, max_bytes_per_file, skip_recall=True)
        if data is None:
            raise ReadBoundaryError("file requires recall; it is not stored locally")
        result.append({"path": name, **data})
    return {"files": result, "count": len(result)}


def compare_text_files(before_path, after_path, roots, *, context_lines=3):
    """Unified diff for two existing Owner-scoped files; no write authority."""
    import difflib
    if (type(before_path) is not str or not before_path
            or type(after_path) is not str or not after_path
            or type(context_lines) is not int or not 0 <= context_lines <= 8):
        raise ReadBoundaryError("invalid diff request")
    if _skip_bulk_file(before_path, roots) or _skip_bulk_file(after_path, roots):
        raise ReadBoundaryError("comparison file requires recall")
    before = _read_inspection_text(before_path, roots, 32768, skip_recall=True)
    if before is None:
        raise ReadBoundaryError("comparison file requires recall")
    after = _read_inspection_text(after_path, roots, 32768, skip_recall=True)
    if after is None:
        raise ReadBoundaryError("comparison file requires recall")
    changes = "".join(difflib.unified_diff(
        before["content"].splitlines(keepends=True),
        after["content"].splitlines(keepends=True),
        fromfile="before", tofile="after", n=context_lines,
    ))
    return {
        "before_sha256": before["sha256"],
        "after_sha256": after["sha256"],
        "changed": before["sha256"] != after["sha256"],
        "diff": changes[:16384],
        "diff_truncated": len(changes) > 16384,
    }

MAX_STREAM_FILE_BYTES = 256 * 1024 * 1024


def read_text_chunk(path, roots, *, offset=-1, max_bytes=16384):
    """Read a UTF-8 slice of a large log, or its last page (offset=-1).

    Returned next_offset can be supplied on a later call to inspect appended
    data, without process execution or opening a new permission scope.
    This is a single-read best-effort snapshot, not a race-proof file handle.
    """
    import os
    if (type(offset) is not int or not -1 <= offset <= MAX_STREAM_FILE_BYTES
            or type(max_bytes) is not int or not 4 <= max_bytes <= 65536):
        raise ReadBoundaryError("invalid stream bounds")
    p = _check(path, roots)
    if not p.is_file():
        raise ReadBoundaryError("not an ordinary file")
    with p.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > MAX_STREAM_FILE_BYTES):
            raise ReadBoundaryError("unsupported log file")
        total = before.st_size
        start = max(0, total - max_bytes) if offset == -1 else offset
        if start > total:
            raise ReadBoundaryError("stream cursor exceeds EOF")
        length = min(max_bytes, total - start)
        stream.seek(start)
        raw = stream.read(length)
        after = os.fstat(stream.fileno())
    if (len(raw) != length
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ReadBoundaryError("stream file changed while reading")
    _verify_named_file(path, roots, after)

    # Tail mode starts at an arbitrary byte. Skip only UTF-8 continuation
    # bytes until the first full character, never fabricate replacement chars.
    if offset == -1 and start > 0:
        skip = 0
        while skip < len(raw) and raw[skip] & 0xC0 == 0x80:
            skip += 1
        raw = raw[skip:]
        start += skip
    elif raw and start > 0 and raw[0] & 0xC0 == 0x80:
        raise ReadBoundaryError("stream cursor splits a UTF-8 character")

    end_of_file = start + len(raw) == total
    # A non-final chunk may stop in the middle of a UTF-8 character. Hold
    # those trailing bytes until the following page and report the true cursor.
    while True:
        try:
            text = raw.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            if end_of_file or exc.reason != "unexpected end of data" or exc.end != len(raw):
                raise ReadBoundaryError("invalid UTF-8 log data") from exc
            raw = raw[:exc.start]
            end_of_file = False
    if not raw and start < total:
        raise ReadBoundaryError("no full UTF-8 character in requested chunk")
    next_offset = start + len(raw)
    return {
        "content": text,
        "start_offset": start,
        "next_offset": next_offset,
        "total_bytes": total,
        "truncated": next_offset < total,
        "prefix_skipped": start > 0,
        "modified_ns": after.st_mtime_ns,
    }

MAX_HASH_FILE_BYTES = 512 * 1024 * 1024


def sha256_file(path, roots):
    """Bounded streamed SHA-256 for a release candidate or large local file.

    Does not authenticate its publisher or authorize install/launch/edit.
    Content is never returned. Caller still needs a separately trusted digest.
    """
    import os
    p = _check(path, roots)
    if not p.is_file():
        raise ReadBoundaryError("not a regular file")
    digest = sha256()
    with p.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > MAX_HASH_FILE_BYTES):
            raise ReadBoundaryError("unsupported hash file")
        length = 0
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            length += len(chunk)
            if length > MAX_HASH_FILE_BYTES:
                raise ReadBoundaryError("hash byte limit exceeded")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (length != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ReadBoundaryError("file changed during hashing")
    _verify_named_file(path, roots, after)
    return {
        "sha256": digest.hexdigest(),
        "size_bytes": length,
        "modified_ns": after.st_mtime_ns,
    }
