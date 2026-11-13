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


def owner_journal_path(repo_root=None, *, storage_override=None):
    """Fixed Owner storage; override is test-only and never from an MCP request."""
    import os
    import stat
    import subprocess
    from pathlib import Path
    storage = (Path(storage_override) if storage_override is not None else
               Path(os.environ["LOCALAPPDATA"]) / "SoraCommanderNext")
    if (not storage.is_absolute() or not storage.is_dir()
            or storage.is_symlink() or storage.is_junction()):
        raise RuntimeError("Owner journal directory invalid")
    if os.name == "nt":
        # Reject inherited or extra ACEs; require exact Owner/SYSTEM/Admins.
        script = r"""
$ErrorActionPreference='Stop'
$acl=Get-Acl -LiteralPath $env:SORA_NEXT_VERIFY_STORAGE
$owner=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed=@($owner,'S-1-5-18','S-1-5-32-544')
if(-not $acl.AreAccessRulesProtected){exit 2}
$rules=@($acl.Access)
if($rules.Count -ne 3){exit 3}
$seen=@()
foreach($r in $rules){
 $sid=$r.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
 if($sid -notin $allowed -or $r.AccessControlType -ne 'Allow' -or
    -not $r.FileSystemRights.HasFlag([System.Security.AccessControl.FileSystemRights]::FullControl) -or
    $r.IsInherited){exit 4}
 $seen+= $sid
}
if(@($seen|Select-Object -Unique).Count -ne 3){exit 5}
$targets=@('owner-session-journal.json','owner-output-archive.json','owner-session-journal.json.lock')
foreach($filename in $targets){
 $target=Join-Path $env:SORA_NEXT_VERIFY_STORAGE $filename
 if(Test-Path -LiteralPath $target){
 $fileAcl=Get-Acl -LiteralPath $target
 # Raw stdout/stderr can contain secrets. File ACLs, like the parent
 # directory, must have inheritance disabled and exact explicit grants.
 if(-not $fileAcl.AreAccessRulesProtected){exit 9}
 $fileRules=@($fileAcl.Access)
 if($fileRules.Count -ne 3){exit 6}
 $fileSeen=@()
 foreach($r in $fileRules){
  $sid=$r.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
  if($sid -notin $allowed -or $r.AccessControlType -ne 'Allow' -or
     -not $r.FileSystemRights.HasFlag([System.Security.AccessControl.FileSystemRights]::FullControl) -or
     $r.IsInherited){
   exit 7
  }
  $fileSeen+=$sid
 }
 if(@($fileSeen|Select-Object -Unique).Count -ne 3){exit 8}
 }
}
Write-Output 'OWNER_ACL_OK'
"""
        env = {**os.environ, "SORA_NEXT_VERIFY_STORAGE": str(storage)}
        # A pwsh 7 parent may provide a module path that prevents Windows
        # PowerShell 5.1 from loading Get-Acl. Preserve the full ACL test.
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as key:
                machine_modules, _ = winreg.QueryValueEx(key, "PSModulePath")
            if type(machine_modules) is not str or not machine_modules.strip():
                raise ValueError("Windows module path unavailable")
            env["PSModulePath"] = os.path.expandvars(machine_modules)
        except (OSError, ValueError) as exc:
            raise RuntimeError("Owner ACL unavailable") from exc
        try:
            from .cli_monitor_bootstrap import _system_executable
            powershell = _system_executable("WindowsPowerShell", "v1.0", "powershell.exe")
            result = subprocess.run([powershell, "-NoProfile", "-NonInteractive",
                                     "-Command", script], capture_output=True,
                                    timeout=10, env=env, text=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Owner ACL unavailable") from exc
        if result.returncode != 0 or result.stdout.strip() != "OWNER_ACL_OK":
            raise RuntimeError("Owner ACL invalid")
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

    from pathlib import Path
    from .policy import Policy
    from .windows_owner import OwnerSessionGuard
    from .windows_owner_controller import WindowsOwnerController
    from .owner_review_dispatch import OwnerReviewDispatcher

    guard = OwnerSessionGuard()
    guard.verify()
    repo_root = Path(__file__).resolve().parents[2]
    # Explicit local dev profile. No remotely chosen paths or implicit grants.
    # The existing default keeps the original tests-only scope.
    profile = os.environ.get("SORA_NEXT_OWNER_PROFILE", "tests")
    try:
        roots = startup_allowed_roots(repo_root, profile, os.environ)
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
        task_presets = load_task_presets(os.environ)
    except TaskPresetError as error:
        import sys
        print(f"[HOLD] tasks.json: {error}", file=sys.stderr, flush=True)
        raise SystemExit(2) from None
    from .ogf_cue import OgfBridgeError, load_ogf_bridge
    try:
        ogf_bridge = load_ogf_bridge(os.environ)
        if ogf_bridge is not None:
            ogf_bridge.startup()
    except (OgfBridgeError, OSError, RuntimeError, ValueError):
        import sys
        print('[HOLD] ogf-bridge.json: configuration or protected storage unavailable', file=sys.stderr, flush=True)
        raise SystemExit(2) from None
    jobs = None
    journal_enabled = os.environ.get("SORA_NEXT_JOURNAL_PILOT") == "1"
    result_enabled = os.environ.get("SORA_NEXT_RESULT_ARCHIVE_PILOT") == "1"
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
    if os.environ.get('SORA_NEXT_EDIT_PILOT') == '1':
        from .edit_review_pilot import EditReviewPilot
        # Creation needs its own explicit local opt-in in addition to the edit pilot.
        # No remote MCP argument can grant it; default remains disabled.
        enable_create = os.environ.get('SORA_NEXT_CREATE_PILOT') == '1'
        host._edit_review_pilot = EditReviewPilot(
            repo_root / 'tests' / 'real_mcp', enable_create=enable_create,
            enable_timeout_dev=True, locale=language)
    serve_stdio(host)


if __name__ == "__main__":
    main()
