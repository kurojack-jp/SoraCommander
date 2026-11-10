"""Fixed-command Git observations. No MCP, approval, write or shell interface."""
import codecs
from functools import lru_cache
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import subprocess
from threading import Thread

from .file_reads import ReadBoundaryError, _check, _read_name_denied

CAP = 65536
MAX_OFFSET = 256 * 1024 * 1024
OPTIONS = (
    "-c", "core.fsmonitor=false", "-c", "core.hooksPath=NUL",
    "-c", "core.pager=cat", "-c", "diff.external=",
    "-c", "core.untrackedCache=false", "-c", "protocol.allow=never",
    "-c", "protocol.https.allow=always", "-c", "credential.helper=",
    "-c", "log.showSignature=false", "-c", "core.quotePath=true",
    "-c", "core.askPass=",
    "--no-pager", "--literal-pathspecs",
)
# Fixed reason words that the MCP layer may return to ChatGPT as they are.
REASON_CODES = frozenset({
    "FILE_OUTSIDE_REPOSITORY", "FILTER_DRIVER_NAME", "GIT_FAILED",
    "GIT_INSTALL_INVALID", "GIT_INSTALL_LINK", "GIT_LAUNCH_FAILED",
    "GIT_NOT_INSTALLED", "GIT_OUTPUT_FAILED", "GIT_OUTPUT_LIMIT", "GIT_TIMEOUT",
    "HTTPS_UPSTREAM_REQUIRED", "INVALID_BOOLEAN", "INVALID_FILE_PATH",
    "INVALID_LIMIT", "INVALID_OFFSET", "INVALID_PATH", "INVALID_PATHS",
    "INVALID_REVISION", "LOCAL_REF_INVALID", "NOT_DIRECTORY", "NOT_REPOSITORY",
    "NO_UPSTREAM", "PROTECTED_FILE", "REMOTE_READ_FAILED",
    "REMOTE_REF_AMBIGUOUS", "TOO_MANY_FILTER_DRIVERS",
})
DIFF_OPTIONS = ("--no-ext-diff", "--no-textconv", "--no-color", "--no-renames")


@lru_cache(maxsize=1)
def fixed_git():
    """Resolve only the two reviewed installation locations, never PATH."""
    for variable in ("ProgramFiles", "ProgramW6432"):
        root = os.environ.get(variable)
        if not root or not Path(root).is_absolute():
            continue
        candidate = Path(root) / "Git" / "cmd" / "git.exe"
        if not candidate.exists():
            continue
        for item in (candidate, *candidate.parents):
            info = item.lstat()
            if (item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400):
                raise ReadBoundaryError("GIT_INSTALL_LINK")
        if not stat.S_ISREG(candidate.stat().st_mode):
            raise ReadBoundaryError("GIT_INSTALL_INVALID")
        return candidate
    raise ReadBoundaryError("GIT_NOT_INSTALLED")


def safe_env(executable):
    install = Path(executable).parent.parent
    result = {key: os.environ[key] for key in
              ("SystemRoot", "TEMP", "TMP", "USERPROFILE", "HOME")
              if key in os.environ}
    result.update(PATH=os.pathsep.join((str(install / "cmd"),
                                       str(install / "mingw64" / "bin"))),
                  LANG="C", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="NUL",
                  GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
                  GIT_ASKPASS="", SSH_ASKPASS="", GIT_PAGER="cat", PAGER="cat",
                  GIT_EXEC_PATH=str(install / "mingw64" / "libexec" / "git-core"))
    return result


def _revision(value):
    if (type(value) is not str or not re.fullmatch(r"[A-Za-z0-9._/@^~-]{1,200}", value)
            or value.startswith("-") or ".." in value or ":" in value):
        raise ReadBoundaryError("INVALID_REVISION")


def _relative(value):
    if (type(value) is not str or not value or len(value) > 4096
            or value.startswith("-") or PureWindowsPath(value).root
            or PureWindowsPath(value).drive or Path(value).is_absolute()
            or any(c in value for c in ("\0", "\n", "\r", ":"))
            or ".." in value.replace("\\", "/").split("/")):
        raise ReadBoundaryError("INVALID_FILE_PATH")


def validate_arguments(name, arguments):
    path = arguments.get("path")
    if type(path) is not str or not path or "\0" in path or "\n" in path:
        raise ReadBoundaryError("INVALID_PATH")
    for key in ("base", "rev", "ref"):
        if arguments.get(key) is not None:
            _revision(arguments[key])
    if name == "git_show":
        _revision(arguments.get("rev"))
    if name == "git_remote_check":
        _revision(arguments.get("ref"))
    for key in ("include_untracked", "stat_only"):
        if key in arguments and type(arguments[key]) is not bool:
            raise ReadBoundaryError("INVALID_BOOLEAN")
    offset = arguments.get("offset", 0)
    if type(offset) is not int or not 0 <= offset <= MAX_OFFSET:
        raise ReadBoundaryError("INVALID_OFFSET")
    limit = arguments.get("limit", 20)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ReadBoundaryError("INVALID_LIMIT")
    paths = arguments.get("paths")
    if paths is not None:
        if type(paths) is not list or len(paths) > 32:
            raise ReadBoundaryError("INVALID_PATHS")
        for value in paths:
            _relative(value)
    if arguments.get("file") is not None:
        _relative(arguments["file"])


MAX_FILTER_DRIVERS = 32
_NO_FILTER_COMMANDS = {"config", "rev-parse", "ls-remote"}


def _filter_overrides(cwd):
    """Blank every configured filter driver for the next command.

    Repository config plus .gitattributes or .git/info/attributes can name
    clean/smudge/process programs that `status`, `diff` and `show` would run.
    `--attr-source` does not cover info/attributes, so empty each driver.
    """
    raw = _run(cwd, ["config", "--null", "--name-only", "--get-regexp", r"^filter\."],
               allow_missing=True)
    names = set()
    for key in filter(None, raw.decode("utf-8", "strict").split("\0")):
        prefix, _, rest = key.partition(".")
        name, _, _variable = rest.rpartition(".")
        if prefix.lower() != "filter" or not name or "=" in name or "\n" in name:
            raise ReadBoundaryError("FILTER_DRIVER_NAME")
        names.add(name)
    if len(names) > MAX_FILTER_DRIVERS:
        raise ReadBoundaryError("TOO_MANY_FILTER_DRIVERS")
    options = []
    for name in sorted(names):
        for variable, value in (("clean", ""), ("smudge", ""), ("process", ""),
                                ("required", "false")):
            options += ["-c", f"filter.{name}.{variable}={value}"]
    return options


def _run(cwd, args, *, offset=0, paged=False, prefix=b"", network=False,
         allow_missing=False):
    """Drain concurrently, retaining one page only; stderr never enters results."""
    executable = fixed_git()
    neutral = [] if args[0] in _NO_FILTER_COMMANDS else _filter_overrides(cwd)
    start, stop = offset, offset + CAP + 4
    captured = bytearray()
    total = 0
    reader_errors = []

    def consume(data):
        nonlocal total
        low, high = max(start - total, 0), min(stop - total, len(data))
        if high > low:
            captured.extend(data[low:high])
        total += len(data)

    def drain():
        try:
            consume(prefix)
            while chunk := process.stdout.read(8192):
                consume(chunk)
        except Exception:
            reader_errors.append(True)

    try:
        with subprocess.Popen([str(executable), *OPTIONS, *neutral, *args], cwd=str(cwd),
                              env=safe_env(executable), shell=False,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
            reader = Thread(target=drain, daemon=True)
            reader.start()
            try:
                code = process.wait(timeout=20 if network else 15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise ReadBoundaryError("GIT_TIMEOUT") from None
            finally:
                reader.join(timeout=1)
            if reader.is_alive() or reader_errors:
                raise ReadBoundaryError("GIT_OUTPUT_FAILED")
            if code == 1 and allow_missing:
                return b""  # `config --get-regexp` found no key.
            if code:
                # ls-remote fails for a private, missing or unreachable remote;
                # credentials are deliberately unavailable to this tool.
                raise ReadBoundaryError("REMOTE_READ_FAILED" if network else "GIT_FAILED")
    except OSError:
        raise ReadBoundaryError("GIT_LAUNCH_FAILED") from None
    if not paged:
        if total > CAP:
            raise ReadBoundaryError("GIT_OUTPUT_LIMIT")
        return bytes(captured)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    content = decoder.decode(bytes(captured[:CAP]), final=total <= offset + CAP)
    pending = decoder.getstate()[0]
    used = min(len(captured), CAP) - len(pending)
    return {"content": content, "offset": offset,
            "next_offset": offset + used if total > offset + used else None}


def _repository(path, roots, with_branch=False):
    folder = _check(path, roots)
    if not folder.is_dir():
        raise ReadBoundaryError("NOT_DIRECTORY")
    values = _run(folder, ["rev-parse", "--show-toplevel", *(["--abbrev-ref", "HEAD"] if with_branch else [])]).decode("utf-8").splitlines()
    top = values[0]
    top = _check(top, roots)
    if not top.is_dir():
        raise ReadBoundaryError("NOT_REPOSITORY")
    return (top, values[1]) if with_branch and len(values) == 2 else top


def _file(top, value, roots):
    _relative(value)
    target = top / value
    # Deleted/historical files still need both lexical and existing-parent checks.
    if any(_read_name_denied(part) for part in Path(value).parts):
        raise ReadBoundaryError("PROTECTED_FILE")
    resolved = target.resolve()
    if not resolved.is_relative_to(top):
        raise ReadBoundaryError("FILE_OUTSIDE_REPOSITORY")
    existing = target
    while not existing.exists() and not existing.is_symlink():
        existing = existing.parent
    _check(str(existing), roots)
    return value


def _paths(top, paths, roots):
    for value in paths or ():
        _file(top, value, roots)
    return ["--", *(paths or ())]


def git_snapshot(path, roots, include_untracked=False):
    validate_arguments("git_snapshot", locals())
    top = _repository(path, roots)
    raw = _run(top, ["status", "--porcelain=v2", "--branch", "--untracked-files=all"])
    lines = raw.decode("utf-8", "replace").splitlines()
    untracked = [line[2:] for line in lines if line.startswith("? ")]
    return {"status": "\n".join(line for line in lines if not line.startswith("? ")),
            "untracked_count": len(untracked),
            "untracked": untracked if include_untracked else [],
            "log": _run(top, ["log", *DIFF_OPTIONS, "--oneline", "-n", "10"]).decode("utf-8", "replace")}


def _patch(top, roots, discovery, command, offset):
    names = _run(top, discovery).decode("utf-8", "strict").split("\0")
    allowed, omitted = [], 0
    for name in filter(None, names):
        try:
            allowed.append(_file(top, name, roots))
        except ReadBoundaryError:
            omitted += 1
    prefix = b"Protected file content omitted.\n" * omitted
    if not allowed:
        content = prefix[offset:offset + CAP].decode("utf-8")
        return {"content": content, "offset": offset,
                "next_offset": offset + len(content) if len(prefix) > offset + CAP else None}
    return _run(top, [*command, "--", *allowed], offset=offset, paged=True, prefix=prefix)


def git_diff(path, roots, base=None, paths=None, stat_only=False, offset=0):
    validate_arguments("git_diff", locals())
    top = _repository(path, roots)
    selection = _paths(top, paths, roots)
    comparison = f"{base}...HEAD" if base is not None else "HEAD"
    common = ["diff", *DIFF_OPTIONS, comparison]
    return _patch(top, roots, [*common, "--name-only", "-z", *selection],
                  [*common, "--stat" if stat_only else "--patch"], offset)


def git_log(path, roots, limit=20, paths=None):
    validate_arguments("git_log", locals())
    top = _repository(path, roots)
    selection = _paths(top, paths, roots)
    return {"content": _run(top, ["log", *DIFF_OPTIONS, f"-n{limit}",
                                "--format=%H%x09%an%x09%ad%x09%s", "--date=iso-strict",
                                *selection]).decode("utf-8", "replace")}


def git_show(path, roots, rev, file=None, offset=0):
    validate_arguments("git_show", locals())
    # Refuse protected names even before repository discovery.
    if file is not None and any(_read_name_denied(p) for p in Path(file).parts):
        raise ReadBoundaryError("PROTECTED_FILE")
    top = _repository(path, roots)
    if file is not None:
        _file(top, file, roots)
        return _run(top, ["show", *DIFF_OPTIONS, f"{rev}:{file}", "--"],
                    offset=offset, paged=True)
    common = ["show", *DIFF_OPTIONS, "--diff-merges=first-parent", rev]
    return _patch(top, roots, [*common, "--format=", "--name-only", "-z", "--"],
                  [*common, "--stat", "--patch"], offset)


def git_remote_check(path, roots, ref):
    validate_arguments("git_remote_check", locals())
    _revision(ref)
    top, branch = _repository(path, roots, with_branch=True)
    # One config process obtains the current branch's upstream name and URLs.
    raw = _run(top, ["config", "--null", "--get-regexp", r"^(branch\..*\.remote|remote\..*\.url)$"],
               allow_missing=True)
    config = dict(item.split("\n", 1) for item in raw.decode("utf-8").split("\0") if item)
    remote = config.get(f"branch.{branch}.remote", "")
    url = config.get(f"remote.{remote}.url", "")
    if not remote or not url:
        raise ReadBoundaryError("NO_UPSTREAM")
    if not re.fullmatch(r"https://[^\s/@:]+(?::[0-9]+)?(?:/[^\s]*)?", url):
        raise ReadBoundaryError("HTTPS_UPSTREAM_REQUIRED")
    # A short name such as "main" means the branch; tags need "refs/tags/...".
    ref = ref if ref.startswith("refs/") else f"refs/heads/{ref}"
    local = _run(top, ["rev-parse", "--verify", ref]).decode("ascii").strip()
    output = _run(top, ["ls-remote", "--refs", url, ref], network=True).decode("ascii").splitlines()
    matches = [line.split("\t") for line in output]
    if len(matches) != 1 or len(matches[0]) != 2 or matches[0][1] != ref:
        raise ReadBoundaryError("REMOTE_REF_AMBIGUOUS")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", local):
        raise ReadBoundaryError("LOCAL_REF_INVALID")
    return {"local_sha": local, "remote_sha": matches[0][0],
            "matches": local == matches[0][0]}
