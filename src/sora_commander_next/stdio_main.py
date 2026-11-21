"""Stdio MCP entrypoint; deny-only by default, explicit Owner review pilot opt-in."""

from .host import Host
from .mcp_adapter import serve_stdio
from .allowed_roots import AllowedRootsError, startup_allowed_roots
from .owner_storage import ensure_owner_storage


def owner_pilot_root(repo_root, profile):
    """Fixed local development scope only; never accept a remote path."""
    from pathlib import Path
    base = Path(repo_root).resolve(strict=True)
    if profile == "tests":
        return base / "tests"
    if profile == "development":
        return base
    raise ValueError("unsupported local Owner profile")


def _verify_owner_journal_acls(storage):
    from .owner_stage_acl import OwnerStageAclError, _check, _sids

    # Name the object and the fixed reason, never the full path.
    label = "the Owner storage folder"
    try:
        import win32security
        allowed = {win32security.ConvertSidToStringSid(sid) for sid in _sids()}
        _check(storage, allowed)
        for name in ("owner-session-journal.json", "owner-output-archive.json",
                     "owner-session-journal.json.lock"):
            target = storage / name
            label = name
            try:
                target.lstat()
            except FileNotFoundError:
                continue
            _check(target, allowed)
    except OwnerStageAclError as exc:
        raise RuntimeError(f"Owner ACL invalid: {label}: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Owner ACL unavailable: {label}") from exc


def owner_journal_path(repo_root=None, *, storage_override=None):
    """Fixed Owner storage; override is test-only and never from an MCP request."""
    import os
    import stat
    from pathlib import Path
    storage = (Path(storage_override) if storage_override is not None else
               Path(os.environ["LOCALAPPDATA"]) / "SoraCommanderNext")
    if (not storage.is_absolute() or not storage.is_dir()
            or storage.is_symlink() or storage.is_junction()):
        raise RuntimeError("Owner journal directory invalid")
    if os.name == "nt":
        _verify_owner_journal_acls(storage)
    target = storage / "owner-session-journal.json"
    if target.is_symlink() or target.is_junction():
        raise RuntimeError("Owner journal target invalid")
    if target.exists():
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("Owner journal target invalid")
    # Both file ACLs were checked by the same local, fixed-scope Owner test.
    # Reject a pre-existing link or hardlink before using raw job output.
    archive = storage / "owner-output-archive.json"
    if archive.is_symlink() or archive.is_junction():
        raise RuntimeError("Owner output archive target invalid")
    if archive.exists():
        info = archive.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("Owner output archive target invalid")
    lock = storage / "owner-session-journal.json.lock"
    if lock.is_symlink() or lock.is_junction():
        raise RuntimeError("Owner journal lock target invalid")
    if lock.exists():
        info = lock.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("Owner journal lock target invalid")
    return target


def main():
    # Explicit local pilot only. Default stdio remains deny-only.
    import os
    if os.environ.get("SORA_NEXT_PRODUCT_BROKER") == "1":
        # Product selection never falls through to the legacy execution/GUI
        # pilots, even when the Broker is unavailable or construction fails.
        if any(os.environ.get(name) == "1" for name in (
                "SORA_NEXT_OWNER_REVIEW_PILOT", "SORA_NEXT_EDIT_PILOT",
                "SORA_NEXT_CREATE_PILOT", "SORA_NEXT_JOURNAL_PILOT",
                "SORA_NEXT_RESULT_ARCHIVE_PILOT")):
            raise RuntimeError("product Broker and legacy pilots are mutually exclusive")
        from .product_composition import build_product_host
        serve_stdio(build_product_host())
        return
    if os.environ.get("SORA_NEXT_OWNER_REVIEW_PILOT") != "1":
        serve_stdio(Host())
        return

    from .owner_journal_lease import OwnerJournalLeaseError, owner_journal_lease
    try:
        with owner_journal_lease(os.environ.get("SORA_NEXT_JOURNAL_PILOT") == "1"):
            # serve_stdio closes the Host in its own finally, before returning.
            serve_stdio(build_owner_pilot_host())
    except OwnerJournalLeaseError as error:
        import sys
        print(f"[HOLD] {error}; stdio pilot not started.", file=sys.stderr, flush=True)
        raise SystemExit(2) from None


def build_owner_pilot_host(repo_root=None, *, environ=None, profile=None,
                           journal_enabled=None, result_enabled=None,
                           edit_enabled=None, create_enabled=None):
    """Shared local composition; journal-enabled callers must hold a lifetime lease.

    The stdio and exec entry points own that lease around construction and serving.
    Keeping it outside this builder avoids event-loop/thread cleanup ownership.
    """
    import os
    environ = os.environ if environ is None else environ

    from pathlib import Path
    from .policy import Policy
    from .windows_owner import OwnerSessionGuard
    from .windows_owner_controller import WindowsOwnerController
    from .owner_review_dispatch import OwnerReviewDispatcher

    guard = OwnerSessionGuard()
    guard.verify()
    repo_root = Path(__file__).resolve().parents[2] if repo_root is None else Path(repo_root)
    # Explicit local dev profile. No remotely chosen paths or implicit grants.
    # The existing default keeps the original tests-only scope.
    profile = environ.get("SORA_NEXT_OWNER_PROFILE", "tests") if profile is None else profile
    try:
        roots = startup_allowed_roots(repo_root, profile, environ)
    except AllowedRootsError as error:
        import sys
        print(f"[HOLD] {error}", file=sys.stderr, flush=True)
        raise SystemExit(2) from None
    from .owner_ui_language import OwnerLanguageError, owner_ui_language
    try:
        language = owner_ui_language()
    except OwnerLanguageError as error:
        import sys
        print(f"[HOLD] {error}", file=sys.stderr, flush=True)
        raise SystemExit(2) from None
    from .task_presets import TaskPresetError, load_task_presets
    try:
        task_presets = load_task_presets(environ)
    except TaskPresetError as error:
        import sys
        print(f"[HOLD] tasks.json: {error}", file=sys.stderr, flush=True)
        raise SystemExit(2) from None
    from .ogf_cue import OgfBridgeError, load_ogf_bridge
    try:
        ogf_bridge = load_ogf_bridge(environ)
        if ogf_bridge is not None:
            ogf_bridge.startup()
    except (OgfBridgeError, OSError, RuntimeError, ValueError):
        import sys
        print('[HOLD] ogf-bridge.json: configuration or protected storage unavailable', file=sys.stderr, flush=True)
        raise SystemExit(2) from None
    jobs = None
    journal_enabled = (environ.get("SORA_NEXT_JOURNAL_PILOT") == "1"
                       if journal_enabled is None else journal_enabled)
    result_enabled = (environ.get("SORA_NEXT_RESULT_ARCHIVE_PILOT") == "1"
                      if result_enabled is None else result_enabled)
    if result_enabled and not journal_enabled:
        raise RuntimeError("Owner result archive requires journal opt-in")
    if journal_enabled:
        from .jobs import JobManager
        ensure_owner_storage()
        journal = owner_journal_path(repo_root)
        archive = (journal.with_name("owner-output-archive.json")
                   if result_enabled else None)
        jobs = JobManager(journal_path=journal, output_archive_path=archive,
                          protected_stage=True)
    host = Host(policy=Policy(roots=roots.full, read_only_roots=roots.read_only),
                owner=guard.sid, jobs=jobs)
    host.attach_owner_review_local(OwnerReviewDispatcher(WindowsOwnerController(host)))
    host.task_presets = task_presets
    host.ogf_bridge = ogf_bridge
    # Explicit secondary opt-in; disposable tests/real_mcp files only.
    edit_enabled = (environ.get('SORA_NEXT_EDIT_PILOT') == '1'
                    if edit_enabled is None else edit_enabled)
    if edit_enabled:
        from .edit_review_pilot import EditReviewPilot
        # Creation needs its own explicit local opt-in in addition to the edit pilot.
        # No remote MCP argument can grant it; default remains disabled.
        enable_create = (environ.get('SORA_NEXT_CREATE_PILOT') == '1'
                         if create_enabled is None else create_enabled)
        host._edit_review_pilot = EditReviewPilot(
            repo_root / 'tests' / 'real_mcp', enable_create=enable_create,
            enable_timeout_dev=True, locale=language)
    return host


if __name__ == "__main__":
    main()
