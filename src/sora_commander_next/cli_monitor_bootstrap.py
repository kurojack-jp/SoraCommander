"""Trusted local CLI monitor bootstrap. Never callable from MCP wire.

Starts the existing Windows Forms monitor from the long-lived Host, before
the reviewed CLI job is confined to a kill-on-close Windows Job Object.
"""
import os
from pathlib import Path
import subprocess
import time

def _tools_for_module(module_file):
    """Resolve bundled tools relative to this trusted package location.

    Source tree: <root>/src/sora_commander_next; installed package:
    <install>/sora_commander_next. No owner-name, drive or repo assumptions.
    Missing packaged tools fail closed in ensure_monitor_for_approved_job.
    This is path discovery, NOT an installer/signature/ACL trust decision.
    """
    module = Path(module_file)
    if module.name != "cli_monitor_bootstrap.py" or module.parent.name != "sora_commander_next":
        raise RuntimeError("unexpected CLI monitor module layout")
    parent = module.parent.parent
    base = parent.parent if parent.name == "src" else parent
    return base / "tools"


_TOOLS = _tools_for_module(__file__)
_WRAPPER = _TOOLS / "Invoke-NextCodexJob.ps1"
_MONITOR = _TOOLS / "cli-monitor.ps1"
_LAUNCHER = _TOOLS / "start-cli-monitor.vbs"


def _system_executable(*components, windows_root=None):
    """Use the actual Windows installation directory, not an assumed C: drive.

    Path construction does not establish OS authenticity. The protected Broker
    must independently attest/allowlist executable identity before launch.
    """
    root = os.environ.get("SystemRoot") if windows_root is None else windows_root
    if type(root) is not str or not root or not Path(root).is_absolute():
        raise RuntimeError("Windows SystemRoot unavailable")
    return str(Path(root, "System32", *components))


def is_monitor_job(request):
    """Only locally recognized wrapper requests, not arbitrary CLI strings."""
    if os.name != "nt":
        return False
    try:
        executable = Path(request.executable)
        if executable.name.lower() not in ("powershell.exe", "pwsh.exe"):
            return False
        argv = tuple(request.argv)
        indices = [i for i, x in enumerate(argv) if x.lower() == "-file"]
        return (len(indices) == 1 and indices[0] + 1 < len(argv)
                and Path(argv[indices[0] + 1]) == _WRAPPER)
    except (AttributeError, TypeError, ValueError):
        return False


def monitor_running():
    """Windows process discovery; fail closed if enumeration fails."""
    # Match the actual -File argument, not an incidental mention of this
    # script in an unrelated PowerShell process' command-line arguments.
    # A false positive would skip the required monitor before a Codex job.
    script = ("$ErrorActionPreference='Stop'; "
              "$target=$env:SORA_NEXT_MONITOR_PATH; "
              "if([string]::IsNullOrWhiteSpace($target)){throw 'monitor path missing'}; "
              "$escaped=[regex]::Escape($target); "
              "$pattern='(?i)(?:^|\\s)-File\\s+(?:\"' + $escaped + '\"|' + "
              "$escaped + ')(?=\\s|$)'; "
              "$otherMode='(?i)(?:^|\\s)-(?:Command|EncodedCommand)(?=\\s|$)'; "
              "$fileSwitch='(?i)(?:^|\\s)-File(?=\\s)'; "
              "$p=@(Get-CimInstance Win32_Process -Filter "
              "\"Name='powershell.exe' OR Name='pwsh.exe'\" | "
              "Where-Object {$_.CommandLine -and $_.CommandLine -match $pattern "
              "-and $_.CommandLine -notmatch $otherMode "
              "-and [regex]::Matches($_.CommandLine,$fileSwitch).Count -eq 1}); "
              "if($p.Count -gt 0){Write-Output 'MONITOR_RUNNING'} "
              "else{Write-Output 'MONITOR_ABSENT'}")
    env = dict(os.environ)
    env["SORA_NEXT_MONITOR_PATH"] = str(_MONITOR)
    result = subprocess.run(
        [_system_executable("WindowsPowerShell", "v1.0", "powershell.exe"),
         "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=12, env=env, text=True)
    if result.returncode != 0 or result.stdout.strip() not in ("MONITOR_RUNNING", "MONITOR_ABSENT"):
        raise RuntimeError("CLI monitor status unknown")
    return result.stdout.strip() == "MONITOR_RUNNING"


def ensure_monitor_for_approved_job(request):
    """Called only inside the trusted Owner approval path, before jobs.start."""
    if not is_monitor_job(request):
        return
    # The release inventory currently includes Python only. Until a trusted,
    # signed tool bundle is installed beside the package, an exact CLI-monitor
    # request must fail closed rather than launching a missing/foreign wrapper.
    if not (_WRAPPER.is_file() and _MONITOR.is_file() and _LAUNCHER.is_file()):
        raise RuntimeError("CLI monitor files missing")
    if monitor_running():
        return
    # Spawned by Host, NOT by the CLI job. Host itself is not in the job object.
    subprocess.Popen(
        [_system_executable("wscript.exe"), "//B", "//Nologo", str(_LAUNCHER)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(16):
        time.sleep(0.25)
        if monitor_running():
            return
    raise RuntimeError("CLI monitor did not start")
