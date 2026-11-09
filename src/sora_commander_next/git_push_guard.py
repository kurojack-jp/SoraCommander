"""Decide which Git requests are refused before any Owner window opens.

Only one push form is reviewable: git push origin <source>:refs/heads/<branch>.
Only the Git commands named below get as far as review at all, because an alias
or a plumbing command can push without the word push. Git never lets an alias
replace a built-in, so a listed name means what it says.

Inside that list, commands that can change the configuration or start a program
are limited to named forms. These are allow-lists because Git also accepts
abbreviated options and joined short options, which a list of bad words misses.
The same applies to commands that discard uncommitted work or change what the
Owner staged: through Next, those are the Owner's own steps.

This reads request text only. It cannot see inside a script or program, know
where the configured origin points, or know what configured hooks and filters
do. The pre-push hook and the Owner's review remain necessary. No I/O, no process.
"""
from pathlib import Path
import re

PROTECTED_BRANCHES = frozenset({"main", "master"})
REVIEWABLE_COMMANDS = frozenset({
    "add", "apply", "bisect", "blame", "branch", "cat-file", "check-attr",
    "check-ignore", "checkout", "cherry-pick", "clean", "commit", "commit-tree", "config",
    "count-objects", "describe", "diff", "diff-index", "diff-tree", "fetch", "for-each-ref",
    "format-patch", "fsck", "grep", "hash-object", "help", "log", "ls-files", "ls-remote",
    "ls-tree", "merge", "merge-base", "mv", "name-rev", "pull", "rebase",
    "reflog", "remote", "rev-list", "rev-parse", "revert", "rm", "show",
    "show-ref", "shortlog", "stash", "status", "submodule", "switch", "tag",
    "var", "version", "write-tree",
})
_SHELLS = frozenset({"cmd", "powershell", "pwsh", "bash", "sh"})
# A leading + forces. A + later in a name is an ordinary ref character.
_ALLOWED_REFSPEC = re.compile(r"([^\s:+*-][^\s:*]*):refs/heads/([^\s:*]+)")
_GIT_THEN_RISKY = re.compile(r"\bgit(?:\.exe)?\b.*\b(?:push|reset|clean|checkout|restore|stash|rm|mv)\b", re.DOTALL)
# Anything else before the command can change the repository, or set a program to run.
_HARMLESS_GLOBALS = frozenset({"--no-pager", "-P", "--version", "--no-optional-locks", "--literal-pathspecs"})
_HARMLESS_CONFIG = ("core.quotepath=", "color.ui=")
_FIRST_WORDS = {
    "bisect": frozenset({"start", "bad", "good", "new", "old", "skip", "reset", "log", "terms"}),
    "submodule": frozenset({"status", "summary"}),
    "remote": frozenset({"show", "get-url"}),
    "reflog": frozenset({"show"}),
}
_REBASE_OPTIONS = frozenset({"--continue", "--abort", "--skip", "--quit", "--onto", "--keep-base", "--autostash",
                             "--no-autostash", "--quiet", "-q", "--verbose", "-v", "--stat", "--no-stat"})
_CONFIG_READ_FLAGS = frozenset({"--get", "--get-all", "--get-regexp", "--list", "-l"})
_CONFIG_READ_MODIFIERS = frozenset({"--local", "--global", "--system", "--worktree", "--show-origin", "--show-scope",
                                    "--name-only", "--null", "-z", "--includes", "--no-includes"})
_CONFIG_WRITE_WORDS = frozenset({"set", "unset", "edit", "rename-section", "remove-section"})
# These name a program for Git to start.
_PROGRAM_OPTIONS = {
    "fetch": ("--upload-pack",),
    "pull": ("--upload-pack",),
    "ls-remote": ("--upload-pack", "--exec"),
    "grep": ("--open-files-in-pager",),
}
_CLEAN_DRY_RUN = frozenset({"-n", "--dry-run"})
_CLEAN_OPTIONS = _CLEAN_DRY_RUN.union({"-d", "-x", "-X", "-q", "--quiet"})
_RM_OPTIONS = frozenset({"-q", "--quiet"})
_APPLY_READ_ONLY = frozenset({"--check", "--stat", "--numstat", "--summary"})
_MV_OPTIONS = frozenset({"-n", "--dry-run", "-v", "--verbose"})
_BRANCH_LIST_OPTIONS = frozenset({"--list", "-l"})
_BRANCH_OPTIONS = _BRANCH_LIST_OPTIONS.union({"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--show-current"})


def _reviewable_push(arguments):
    if len(arguments) != 2 or arguments[0] != "origin":
        return False
    match = _ALLOWED_REFSPEC.fullmatch(arguments[1])
    return match is not None and match.group(2).lower() not in PROTECTED_BRANCHES


def _abbreviates(argument, option):
    head = argument.split("=", 1)[0].lower()
    return len(head) > 2 and option.startswith(head)


def _config_only_reads(arguments):
    flags = [argument for argument in arguments if argument.startswith("-")]
    words = [argument for argument in arguments if not argument.startswith("-")]
    if not _CONFIG_READ_FLAGS.union(_CONFIG_READ_MODIFIERS).issuperset(flags):
        return False
    if words[:1] in (["get"], ["list"]):
        return True
    if _CONFIG_READ_FLAGS.intersection(flags):
        return not _CONFIG_WRITE_WORDS.intersection(words)
    return len(words) == 1 and "." in words[0]


def _before_paths(arguments):
    return arguments[:arguments.index("--")] if "--" in arguments else arguments


def _looks_like_file(path):
    """Refuse what plainly selects many files. A folder named like a file still passes; text cannot tell."""
    last = path.replace("\\", "/").rsplit("/", 1)[-1]
    return (not path.startswith(":") and not set("*?[").intersection(path)
            and "." in last and bool(last.strip(".")))


def _names_files(paths):
    return len(paths) > 0 and all(_looks_like_file(path) for path in paths)


def _only_options(arguments, allowed):
    """Everything before a -- that starts with a dash must be named. Paths after it are free."""
    return all(not argument.startswith("-") or argument in allowed for argument in _before_paths(arguments))


def _new_branch_form(arguments, flag):
    return (len(arguments) in (2, 3) and arguments[0] == flag
            and not any(argument.startswith("-") for argument in arguments[1:]))


def _switch_keeps_work(arguments):
    return ((len(arguments) == 1 and not arguments[0].startswith("-"))
            or _new_branch_form(arguments, "-c"))


def _clean_only_lists(arguments):
    return (_only_options(arguments, _CLEAN_OPTIONS)
            and bool(_CLEAN_DRY_RUN.intersection(_before_paths(arguments))))


def _commit_names_its_paths(arguments):
    """Only -m <text>... -- <path>.... Without paths a commit takes everything staged."""
    index = 0
    while index + 1 < len(arguments) and arguments[index] == "-m":
        index += 2
    return (index > 0 and index < len(arguments) and arguments[index] == "--"
            and _names_files(arguments[index + 1:]))


def _add_names_files(arguments):
    return len(arguments) > 0 and arguments[0] == "--" and _names_files(arguments[1:])


def _rm_names_files(arguments):
    before = _before_paths(arguments)
    return (_only_options(arguments, _RM_OPTIONS)
            and _names_files([argument for argument in before if not argument.startswith("-")]
                             + list(arguments[len(before) + 1:])))


def _apply_only_reads(arguments):
    return (_only_options(arguments, _APPLY_READ_ONLY)
            and bool(_APPLY_READ_ONLY.intersection(_before_paths(arguments))))


def _branch_keeps_refs(arguments):
    flags = [argument for argument in arguments if argument.startswith("-")]
    words = [argument for argument in arguments if not argument.startswith("-")]
    if not _BRANCH_OPTIONS.issuperset(flags):
        return False
    if flags:
        return not words or bool(_BRANCH_LIST_OPTIONS.intersection(flags))
    return len(words) <= 2


_ALLOWED_FORMS = {
    "checkout": lambda arguments: _new_branch_form(arguments, "-b"),
    "switch": _switch_keeps_work,
    "stash": lambda arguments: len(arguments) > 0 and arguments[0] in ("list", "show"),
    "clean": _clean_only_lists,
    "rm": _rm_names_files,
    "add": _add_names_files,
    "apply": _apply_only_reads,
    "mv": lambda arguments: _only_options(arguments, _MV_OPTIONS),
    "commit": _commit_names_its_paths,
    "branch": _branch_keeps_refs,
}


def _form_refused(command, arguments):
    """A listed command used to change configuration, start a program or write any file."""
    # --output can overwrite .git/config or the hook, so it is refused for every command.
    if any(_abbreviates(argument, "--output") for argument in arguments):
        return True
    if command in _ALLOWED_FORMS:
        return not _ALLOWED_FORMS[command](arguments)
    if command == "config":
        return not _config_only_reads(arguments)
    if command == "rebase":
        return any(argument.startswith("-") and argument.split("=", 1)[0] not in _REBASE_OPTIONS
                   for argument in arguments)
    if command in _FIRST_WORDS:
        word = next((argument for argument in arguments if not argument.startswith("-")), None)
        return word is not None and word not in _FIRST_WORDS[command]
    if command == "grep" and any(argument.startswith("-") and not argument.startswith("--") and "O" in argument
                                 for argument in arguments):
        return True
    return any(_abbreviates(argument, option) for argument in arguments
               for option in _PROGRAM_OPTIONS.get(command, ()))


def _git_refused(argv):
    index, has_global = 0, False
    while index < len(argv) and argv[index].startswith("-"):
        option = argv[index]
        has_global = True
        if option == "-c":
            value = argv[index + 1] if index + 1 < len(argv) else ""
            if not value.lower().startswith(_HARMLESS_CONFIG):
                return True
            index += 2
        elif option in _HARMLESS_GLOBALS:
            index += 1
        else:
            return True
    if index >= len(argv):
        return False
    command, arguments = argv[index], argv[index + 1:]
    if command.lower() == "push":
        return has_global or not _reviewable_push(arguments)
    if command not in REVIEWABLE_COMMANDS:
        return True
    return _form_refused(command, arguments)


def push_refused(executable, argv):
    """True means refuse now. False means this rule has no opinion."""
    name = Path(executable).stem.lower()
    if name == "git-push":
        return True
    if name == "git":
        return _git_refused(argv)
    if name in _SHELLS:
        return _GIT_THEN_RISKY.search(" ".join(argv).lower()) is not None
    return False
