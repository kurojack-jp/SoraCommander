"""Update an idle public Windows checkout to its latest official release tag.

--check fetches tags and updates remote-tracking refs, then stops before merging.
Exit codes are 0 for OK or updated, 2 for HOLD, and 6 for an unsupported checkout path.
"""

import argparse
import csv
import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).absolute().parents[1]
if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from tools.scn_broker_autostart import TASK_NAME as BROKER_TASK
from tools.scn_muse_autostart import TASK_NAME as MUSE_TASK
from tools.scn_tunnel_client import checkout_path_hold

# Written as the exact public clone URL, the one form the public scan allows.
OFFICIAL = "https://github.com/kurojack-jp/SoraCommander.git".removesuffix(".git")
VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
EXTERNAL_DRIVER = r"^(filter\..*\.(clean|smudge|process)|merge\..*\.driver)$"
FETCH_REFSPECS = {
    f"{force}refs/heads/{ref}:refs/remotes/origin/{ref}"
    for force in ("", "+") for ref in ("*", "main")
}
GIT_OPTIONS = (
    "-c", "core.fsmonitor=false", "-c", "core.hooksPath=NUL",
    "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
    "-c", "credential.helper=", "-c", "core.askPass=",
    "-c", "credential.interactive=false", "-c", "http.followRedirects=false",
    "-c", "fetch.prune=false", "-c", "fetch.pruneTags=false",
    "-c", "remote.origin.prune=false", "-c", "remote.origin.pruneTags=false",
    "-c", "fetch.recurseSubmodules=false", "-c", "submodule.recurse=false",
    "-c", "merge.autoStash=false", "--no-pager",
)
RUNTIME_MARKERS = (
    r"\.venv\scripts\python", r"\tools\start-",
    "sora_commander_next", "scn_muse_bridge.py", "scn_muse_autostart.py",
    "scn_broker_autostart.py", "tunnel-client.exe", "watch-tunnel",
)
PROCESS_NAMES = {"python.exe", "pythonw.exe", "py.exe", "tunnel-client.exe"}
RELATIVE_MARKERS = re.compile(
    r"(?<![\w.-])(?:scn_(?:muse_bridge|muse_autostart|broker_autostart)\.py"
    r"|sora_commander_next(?:\.\w+)*)"
    r"(?![\w.-])"
)
LAUNCHER_MARKERS = re.compile(
    r"(?<![\w.-])(?:start-(?:broker(?:-tunnel)?|vm-tunnel|usual-mode|owner-broker-dev"
    r"|sora-commander-(?:broker-dev|local))|mcp-(?:broker-client|direct)(?:-vm)?"
    r"|next-development(?:-broker-client)?)\.cmd(?![\w.-])"
)
LOGON_TASKS = (BROKER_TASK, MUSE_TASK)
STOP_TASKS = tuple(f'schtasks /End /TN "{name}"' for name in LOGON_TASKS)
PACKAGE_CHECK = """
from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path.cwd() / "tools"))
import scn_setup

root = Path.cwd()
command = scn_setup.prepare_python(root, True, os.environ)
if command is None or scn_setup.run(
    [*command, "-c", scn_setup.PACKAGE_PROBE, *scn_setup.REQUIREMENTS],
    root, quiet=True,
):
    sys.exit(2)
"""


class UpdateHold(Exception):
    """A precondition or update step failed."""


def run(command, root, *, env=None, timeout=30, encoding="utf-8"):
    return subprocess.run(
        command, cwd=root, env=env, timeout=timeout, check=False,
        stdin=subprocess.DEVNULL, capture_output=True, encoding=encoding, errors="replace",
    )


def fixed_git():
    # Imported on first use so the checkout path check in main() runs first.
    from sora_commander_next.git_readonly import fixed_git as resolve
    return resolve()


def git(root, *args, allow_one=False, failure=None):
    from sora_commander_next.git_readonly import ReadBoundaryError

    try:
        executable = fixed_git()
    except ReadBoundaryError as error:
        reason = str(error)
        if reason == "GIT_NOT_INSTALLED":
            raise UpdateHold("Install Git for Windows in its default folder.") from error
        raise UpdateHold(f"Git for Windows installation refused ({reason}).") from error

    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_ASKPASS="",
        SSH_ASKPASS="", GCM_INTERACTIVE="Never",
    )
    result = run([str(executable), *GIT_OPTIONS, *args], root, env=env, timeout=120)
    if result.returncode == 1 and allow_one:
        return None
    if result.returncode:
        raise UpdateHold(failure or f"Git {args[0]} failed (exit {result.returncode}).")
    return result.stdout.strip()


def clean_tree(root):
    masked = [row for row in git(root, "ls-files", "-v").splitlines()
              if row.startswith("S ") or row[:1].islower()]
    if masked:
        raise UpdateHold("Inspect index entries that Git can skip checking:\n" + "\n".join(masked))
    dirty = git(root, "status", "--porcelain=v1", "-uall", "--ignore-submodules=none")
    if dirty:
        raise UpdateHold("The work tree has local changes:\n" + dirty)


def check_checkout(root):
    if git(root, "rev-parse", "--show-prefix"):
        raise UpdateHold("Run the helper from the root of a Git checkout.")
    if git(root, "rev-parse", "--is-inside-work-tree") != "true":
        raise UpdateHold("A Git work tree is required.")
    if git(root, "branch", "--show-current") != "main":
        raise UpdateHold("The checked out branch must be main.")
    if (
        git(root, "rev-parse", "--symbolic-full-name", "@{upstream}",
            failure="main must track origin/main.") != "refs/remotes/origin/main"
        or git(root, "remote", "get-url", "--all", "origin") not in (OFFICIAL, OFFICIAL + ".git")
    ):
        raise UpdateHold("main must track origin/main on the official HTTPS repository.")
    if git(root, "config", "--get-all", "remote.origin.fetch") not in FETCH_REFSPECS:
        raise UpdateHold("origin must use a standard fetch refspec for origin/main.")
    if git(root, "config", "--name-only", "--get-regexp", EXTERNAL_DRIVER,
           allow_one=True):
        raise UpdateHold("External Git filter or merge drivers are configured.")
    clean_tree(root)
    print("[OK] Clean main checkout with the official HTTPS upstream.")


def _probe(root, *args):
    # update.cmd uses the system interpreter. The checkout venv owns pywin32.
    return run([str(root / ".venv" / "Scripts" / "python.exe"), "-I", "-B",
                str(root / "tools" / "scn_update.py"), *args], root)


def _process_rows():
    from win32com.client import GetObject

    wmi = GetObject(r"winmgmts:\\.\root\cimv2")
    fields = ("Name", "ProcessId", "ExecutablePath", "CommandLine")
    return [{key: row.Properties_(key).Value for key in fields}
            for row in wmi.ExecQuery(f"SELECT {', '.join(fields)} FROM Win32_Process")]


def _task_state(name):
    from win32com.client import Dispatch

    scheduler = Dispatch("Schedule.Service")
    scheduler.Connect()
    return scheduler.GetFolder("\\").GetTask(name).State


def system_binary(relative):
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / relative)


def _process_matches(root, rows, own_pids):
    try:
        if not isinstance(rows, list) or not rows:
            raise ValueError
        prefix = str(root).replace("/", "\\").casefold().rstrip("\\") + "\\"
        matches = []
        for row in rows:
            name = row["Name"].casefold()
            pid = row["ProcessId"]
            if type(pid) is not int or not re.fullmatch(r"[\w.-]+", name):
                raise ValueError
            if pid in own_pids:
                continue
            path = (row["ExecutablePath"] or "").replace("/", "\\").casefold()
            command = (row["CommandLine"] or "").replace("/", "\\").casefold()
            text = f"{path} {command}"
            # CIM does not expose another process's working directory. A relative
            # entry point is enough to HOLD, even for a different checkout.
            if (name == "tunnel-client.exe"
                    or prefix in text and any(marker in text for marker in RUNTIME_MARKERS)
                    or name == "cmd.exe" and LAUNCHER_MARKERS.search(command)
                    or name in PROCESS_NAMES and RELATIVE_MARKERS.search(command)
                    or name in PROCESS_NAMES and not command
                    and (not path or prefix in path)):
                matches.append(f"{name} (PID {pid})")
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise UpdateHold("Could not read the Windows process listing.") from error
    return matches


def _probe_main(args):
    try:
        if len(args) == 2 and args[0] == "--query-processes":
            # Exclude the updater, this worker, and its Windows venv redirector.
            own_pids = {int(args[1]), os.getpid(), os.getppid()}
            value = _process_matches(ROOT, _process_rows(), own_pids)
        elif len(args) == 2 and args[0] == "--query-task" and args[1] in LOGON_TASKS:
            value = _task_state(args[1])
            if type(value) is not int:
                return 1
        else:
            return 1
        print(json.dumps(value))
        return 0
    except UpdateHold:
        return 2
    except Exception:
        # COM and import failures cannot attest idle state. Keep arguments private.
        return 1


def running_processes(root):
    result = _probe(root, "--query-processes", str(os.getpid()))
    if result.returncode == 2:
        raise UpdateHold("Could not read the Windows process listing.")
    if result.returncode:
        raise UpdateHold(f"Could not inspect Windows processes (exit {result.returncode}).")
    try:
        matches = json.loads(result.stdout)
        if not isinstance(matches, list) or not all(
                isinstance(item, str) and re.fullmatch(r"[\w.-]+ \(PID \d+\)", item)
                for item in matches):
            raise ValueError
    except ValueError as error:
        raise UpdateHold("Could not read the Windows process listing.") from error
    return matches


def require_idle_tasks(root):
    for name in LOGON_TASKS:
        result = run([system_binary("schtasks.exe"), "/Query", "/TN", name,
                      "/FO", "CSV", "/NH"], root, encoding="oem")
        if result.returncode == 1:
            missing = ctypes.FormatError(2).strip().casefold()
            if missing and result.stderr.strip().casefold().endswith(missing):
                continue
        if result.returncode:
            raise UpdateHold(f"Could not inspect logon task {name}.")
        rows = list(csv.reader(result.stdout.splitlines()))
        if len(rows) != 1 or len(rows[0]) != 3 or rows[0][0] != "\\" + name:
            raise UpdateHold(f"Could not read logon task state for {name}.")
        state = rows[0][2].strip().casefold()
        if state in ("ready", "disabled"):
            continue
        if state not in ("running", "queued"):
            # CSV status strings are localized. Confirm unfamiliar strings with
            # Task Scheduler's numeric state (1 disabled, 3 ready).
            numeric = _probe(root, "--query-task", name)
            if not numeric.returncode and numeric.stdout.strip() in ("1", "3"):
                continue
        raise UpdateHold(f"Logon task {name} is active or its idle state could not be verified.")


def require_idle_journal():
    from sora_commander_next.owner_journal_lease import OwnerJournalLeaseError, owner_journal_lease

    try:
        with owner_journal_lease():
            pass
    except OwnerJournalLeaseError as error:
        raise UpdateHold("Owner journal is held or could not be checked.") from error


def require_idle(root):
    running = running_processes(root)
    if running:
        print("[HOLD] Stop the following processes before updating:")
        for process in running:
            print(f"  {process}")
        print("[TODO] End any active logon tasks from this checkout:")
        for command in STOP_TASKS:
            print(f"  {command}")
        print("[TODO] For interactive launches, press Ctrl+C in the tunnel window, "
              "then in the Broker window. Press Ctrl+C in the Muse bridge window.")
        raise UpdateHold("Processes still running or could not be inspected.")
    require_idle_tasks(root)
    require_idle_journal()


def release_tags(root):
    output = git(root, "tag", "--format=%(objecttype) %(refname:strip=2)")
    tags = {}
    for row in output.splitlines():
        kind, _, tag = row.partition(" ")
        match = VERSION.fullmatch(tag)
        if kind == "tag" and match:
            tags[tag] = tuple(map(int, match.groups()))
    if not tags:
        raise UpdateHold("No annotated vX.Y.Z release tag was found.")
    return sorted(tags, key=tags.__getitem__)


def check_packages(root, release):
    # Run the newly checked-out setup package step in a fresh interpreter.
    # Its check mode skips installs. The additional probe makes TODOs a HOLD.
    result = run([sys.executable, "-I", "-B", "-c", PACKAGE_CHECK], root)
    if result.returncode:
        raise UpdateHold(f"The code is already at {release}, but packages could not be "
                         r"verified offline. Run tools\setup.cmd before restarting.")
    print("[OK] The updated setup package requirements are satisfied.")


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fetch and preview the update")
    args = parser.parse_args(argv)
    if message := checkout_path_hold(root):
        print(f"[HOLD] {message}")
        return 6

    try:
        check_checkout(root)
        require_idle(root)
        print("[OK] No running processes from this checkout detected.")
        git(root, "fetch", "--tags", "--no-auto-maintenance", "origin",
            failure="Fetch from the official HTTPS upstream failed.")
        print("[DONE] Fetched tags and updated remote-tracking refs.")
        tags = release_tags(root)
        latest = tags[-1]
        tag_ref = f"refs/tags/{latest}"
        tag_object = git(root, "rev-parse", "--verify", tag_ref)
        advertised = git(root, "ls-remote", "--exit-code", "--refs", "origin", tag_ref,
                         failure=f"Could not verify release tag {latest} on the official HTTPS origin.")
        if advertised != f"{tag_object}\t{tag_ref}":
            raise UpdateHold(f"Release tag {latest} differs from the official HTTPS origin.")
        target = git(root, "rev-parse", "--verify", f"{tag_object}^{{commit}}")
        if git(root, "merge-base", "--is-ancestor", target, "refs/remotes/origin/main",
               allow_one=True) is None:
            raise UpdateHold(f"Release tag {latest} is not reachable from origin/main.")

        print(f"[OK] Latest release: {latest}.")
        head = git(root, "rev-parse", "HEAD")
        if head == target or git(root, "merge-base", "--is-ancestor", target, head,
                                 allow_one=True) is not None:
            print(f"[OK] Already current. No update needed for {latest}.")
            if not args.check:
                check_packages(root, latest)
            return 0

        if git(root, "merge-base", "--is-ancestor", head, target, allow_one=True) is None:
            raise UpdateHold(f"Local main cannot fast-forward to {latest}. Update by hand.")
        new_tags = set(git(root, "tag", "--merged", target,
                           "--no-merged", "HEAD").splitlines())
        changes = ", ".join(tag for tag in tags if tag in new_tags) or "(none)"
        print(f"[OK] Incoming release tags: {changes}.")
        print(f"[TODO] Read the release notes: {OFFICIAL}/releases/tag/{latest}")
        if args.check:
            print("[OK] Preview complete.")
            return 0

        # Fetch can take time. Recheck local edits and process state before merging.
        check_checkout(root)
        require_idle(root)
        git(root, "merge", "--ff-only", "--no-overwrite-ignore", target,
            failure="Fast-forward failed. Inspect Git status before continuing.")
        print(f"[DONE] Fast-forwarded main to release {latest}.")
        check_packages(root, latest)
        print(r"[TODO] Restart Broker mode with tools\start-broker-mode.cmd or its logon task.")
        print(r"[TODO] If you use usual mode, restart it with tools\start-usual-mode.cmd.")
        print(r"[TODO] Restart the Muse bridge with its launcher or logon task.")
        print("[TODO] Refresh tools in ChatGPT and Muse.")
        print("[TODO] Read the release notes for action needed items.")
        return 0
    except (OSError, subprocess.TimeoutExpired, UpdateHold) as error:
        print(f"[HOLD] {error}")
        return 2


if __name__ == "__main__":
    if sys.argv[1:2] in (["--query-processes"], ["--query-task"]):
        raise SystemExit(_probe_main(sys.argv[1:]))
    raise SystemExit(main())
