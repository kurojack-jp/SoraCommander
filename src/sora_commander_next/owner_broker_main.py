"""DEV_ONLY Owner Broker process. Started by hand, never by the Host or a tool.

It holds what the MCP Host holds today: jobs, Owner review windows and file
writers, on the same opt-in switches. It answers one Owner over the local pipe.
remote_ready stays false. Closing it stops its jobs, as closing the Host does now.
With the journal on, launches are also recorded in owner-launch-ledger.sqlite.
"""
import argparse
import os
from pathlib import Path
import sys
import time

from .edit_review_pilot import EditReviewPilot
from .host import Host
from .owner_broker_dev import OwnerBrokerBackend
from .owner_broker_pipe import OwnerBrokerPipeServer, broker_pipe_name
from .owner_broker_service import OwnerBrokerService
from .owner_launch_ledger import DurableJobManager, LaunchLedger, LaunchLedgerError
from .owner_review_dispatch import OwnerReviewDispatcher
from .policy import Policy
from .stdio_main import owner_journal_path
from .allowed_roots import AllowedRootsError, startup_allowed_roots
from .owner_storage import ensure_owner_storage
from .windows_owner import OwnerSessionGuard
from .windows_owner_controller import WindowsOwnerController


LEDGER_NAME = "owner-launch-ledger.sqlite"


def broker_is_running(pipe_name):
    """A running Broker's jobs look unresolved from here, so never act beside one."""
    prefix = chr(92) * 2 + "." + chr(92) + "pipe" + chr(92)
    return pipe_name[len(prefix):] in os.listdir(prefix)


def run_ledger_command(ledger, resolve_id, *, confirm=input, write=print):
    """Local only: list unresolved launches, or clear one after a typed yes."""
    rows = ledger.unresolved()
    if resolve_id is None:
        for row in rows:
            started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["started_at"]))
            write(f'{row["request_id"]}  {started}  {row["program"]}')
        write(f"[INFO] {len(rows)} launch(es) with an unknown outcome.")
        return 0
    if resolve_id not in {row["request_id"] for row in rows}:
        write("[HOLD] That request ID is not an unresolved launch.")
        return 2
    write("Clear this only after checking the command's effects by hand.")
    try:
        answer = confirm("Type yes to allow this command again: ")
    except EOFError:  # Windows reports NUL as a console, and it gives no answer.
        answer = ""
    if answer != "yes":
        write("[INFO] Not changed.")
        return 2
    if not ledger.resolve_local(resolve_id):
        write("[HOLD] Not changed.")
        return 2
    write("[OK] Cleared.")
    return 0


def build_backend_host(guard, repo_root, environ):
    """Same composition as the stdio pilot, on the same switches."""
    roots = startup_allowed_roots(
        repo_root, environ.get("SORA_NEXT_OWNER_PROFILE", "tests"), environ)
    jobs = None
    journal_enabled = environ.get("SORA_NEXT_JOURNAL_PILOT") == "1"
    result_enabled = environ.get("SORA_NEXT_RESULT_ARCHIVE_PILOT") == "1"
    if result_enabled and not journal_enabled:
        raise RuntimeError("Owner result archive requires journal opt-in")
    if journal_enabled:
        ensure_owner_storage()
        journal = owner_journal_path(repo_root)
        archive = journal.with_name("owner-output-archive.json") if result_enabled else None
        jobs = DurableJobManager(ledger=LaunchLedger(journal.with_name(LEDGER_NAME)),
                                 journal_path=journal, output_archive_path=archive,
                                 protected_stage=True)
    host = Host(policy=Policy(roots=roots.full, read_only_roots=roots.read_only),
                owner=guard.sid, jobs=jobs)
    host.attach_owner_review_local(OwnerReviewDispatcher(WindowsOwnerController(host)))
    if environ.get("SORA_NEXT_EDIT_PILOT") == "1":
        from .owner_ui_language import owner_ui_language
        host._edit_review_pilot = EditReviewPilot(
            Path(repo_root) / "tests" / "real_mcp",
            enable_create=environ.get("SORA_NEXT_CREATE_PILOT") == "1",
            enable_timeout_dev=True, locale=owner_ui_language())
    return host


def main(argv=None):
    parser = argparse.ArgumentParser(description="DEV_ONLY Owner Broker")
    parser.add_argument("--pipe-name", help="test override under the fixed pipe prefix")
    parser.add_argument("--list-unknown", action="store_true",
                        help="list launches with an unknown outcome, then exit")
    parser.add_argument("--resolve-unknown", metavar="REQUEST_ID",
                        help="clear one unknown launch after a typed yes, then exit")
    arguments = parser.parse_args(argv)
    guard = OwnerSessionGuard()
    guard.verify()
    repo_root = Path(__file__).resolve().parents[2]
    if arguments.resolve_unknown and not sys.stdin.isatty():
        print("[HOLD] Type the answer at a console. Piped input cannot clear a launch.",
              flush=True)
        return 3
    if arguments.list_unknown or arguments.resolve_unknown:
        if broker_is_running(arguments.pipe_name or broker_pipe_name(
                guard.sid, guard._identity.session_id)):
            print("[HOLD] Stop the Broker first. Its running jobs would look unresolved.",
                  flush=True)
            return 3
        ensure_owner_storage()
        ledger = LaunchLedger(owner_journal_path(repo_root).with_name(LEDGER_NAME))
        try:
            return run_ledger_command(ledger, arguments.resolve_unknown)
        finally:
            ledger.close()
    from .owner_ui_language import (OwnerLanguageError, owner_ui_language,
                                    owner_ui_language_source)
    try:
        language = owner_ui_language()
    except OwnerLanguageError as error:
        print(f"[HOLD] {error}", flush=True)
        return 2
    try:
        host = build_backend_host(guard, repo_root, os.environ)
    except AllowedRootsError as error:
        print(f"[HOLD] {error}", flush=True)
        return 2
    except LaunchLedgerError:
        print("[HOLD] The launch ledger could not be opened. Nothing was started.", flush=True)
        return 5
    ledger = getattr(host.jobs, "_ledger", None)
    if ledger is not None and ledger.unresolved():
        print(f"[WARN] {len(ledger.unresolved())} earlier launch(es) have an unknown outcome. "
              "The same commands stay refused until cleared with --resolve-unknown.", flush=True)
    name = arguments.pipe_name or broker_pipe_name(guard.sid, guard._identity.session_id)
    server = OwnerBrokerPipeServer(owner_guard=guard,
                                   service=OwnerBrokerService(OwnerBrokerBackend(host)),
                                   pipe_name=name)
    try:
        server.start()
    except OSError:
        host.close()
        print("[HOLD] The Broker pipe could not be created. Is another Broker running?",
              flush=True)
        return 3
    print("[READY] Owner Broker is running. DEV only. Press Ctrl+C here to stop it.",
          flush=True)
    print(f"[INFO] Allowed folders: {len(host.policy.roots)} full, "
          f"{len(host.policy.read_only_roots)} read-only for file tools", flush=True)
    print(f"[INFO] Window language: {language} ("
          f"{'language.txt' if owner_ui_language_source() == 'file' else 'Windows setting'})",
          flush=True)
    try:
        for _ in sys.stdin:
            pass  # Only end of input or Ctrl+C stops the Broker.
    except KeyboardInterrupt:
        pass
    finally:
        stopped = server.stop()
        host.close()
    print("[STOPPED] Owner Broker stopped." if stopped
          else "[HOLD] A request was still running when the Broker stopped.", flush=True)
    return 0 if stopped else 4


if __name__ == "__main__":
    sys.exit(main())
