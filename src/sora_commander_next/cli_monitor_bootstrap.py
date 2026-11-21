"""Trusted local CLI monitor bootstrap. Never callable from MCP wire.

Starts the existing Windows Forms monitor from the long-lived Host, before
the reviewed CLI job is confined to a kill-on-close Windows Job Object.
"""
import os
from pathlib import Path
import stat
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
_PWSH7 = r"C:\Program Files\PowerShell\7\pwsh.exe"


def _system_executable(*components, windows_root=None):
    """Use the actual Windows installation directory, not an assumed C: drive.

    Path construction does not establish OS authenticity. The protected Broker
    must independently attest/allowlist executable identity before launch.
    """
    root = os.environ.get("SystemRoot") if windows_root is None else windows_root
    if type(root) is not str or not root or not Path(root).is_absolute():
        raise RuntimeError("Windows SystemRoot unavailable")
    return str(Path(root, "System32", *components))


def monitor_shell():
    candidates = [_PWSH7]
    if Path(os.environ.get("SystemRoot", "")).is_absolute():
        candidates.insert(0, _system_executable("WindowsPowerShell", "v1.0", "powershell.exe"))
    for executable in candidates:
        try:
            info = os.lstat(executable)
        except (FileNotFoundError, NotADirectoryError):
            continue
        if stat.S_ISREG(info.st_mode) and not info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return executable
    raise RuntimeError("No supported PowerShell found")


def _monitor_environment(executable):
    # Windows environment lookup is case-insensitive, including in the child.
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    if executable != _PWSH7:
        env["PSModulePath"] = str(Path(executable).with_name("Modules"))
    return env


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


def _query_monitor():
    from win32api import CommandLineToArgv
    from win32com.client import GetObject

    wmi = GetObject(r"winmgmts:\\.\root\cimv2")
    processes = wmi.ExecQuery("SELECT CommandLine FROM Win32_Process "
                              "WHERE Name='powershell.exe' OR Name='pwsh.exe'")
    target = str(_MONITOR).lower()
    for process in processes:
        line = process.CommandLine
        if not line:
            continue
        args = [arg.lower() for arg in CommandLineToArgv(line)[1:]]
        if args.count("-file") != 1 or "-command" in args or "-encodedcommand" in args:
            continue
        index = args.index("-file") + 1
        if index < len(args) and args[index] == target:
            return True
    return False


def monitor_running():
    """Query WMI in the calling thread's COM apartment, failing closed."""
    try:
        import pythoncom

        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        try:
            return _query_monitor()
        finally:
            pythoncom.CoUninitialize()
    except Exception as exc:
        raise RuntimeError("CLI monitor status unknown") from exc


def ensure_monitor_for_approved_job(request):
    """Called only inside the trusted Owner approval path, before jobs.start."""
    if not is_monitor_job(request):
        return
    # The release inventory currently includes Python only. Until a trusted,
    # signed tool bundle is installed beside the package, an exact CLI-monitor
    # request must fail closed rather than launching a missing/foreign wrapper.
    if not (_WRAPPER.is_file() and _MONITOR.is_file()):
        raise RuntimeError("CLI monitor files missing")
    if monitor_running():
        return
    # Spawned by Host, NOT by the CLI job. Host itself is not in the job object.
    executable = monitor_shell()
    subprocess.Popen(
        [executable,
         "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
         "-File", str(_MONITOR)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW, env=_monitor_environment(executable))
    for _ in range(16):
        time.sleep(0.25)
        if monitor_running():
            return
    raise RuntimeError("CLI monitor did not start")
