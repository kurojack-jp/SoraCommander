"""Install, remove, or run the current user's read-only Muse logon task."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import ctypes
from datetime import datetime, timezone
import ipaddress
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from xml.sax.saxutils import escape


CHECKOUT = Path(__file__).resolve().parents[1]
TASK_NAME = "SoraCommander Muse bridge (read-only)"
LOG_NAME = "muse-bridge.log"
LOG_LIMIT = 1024 * 1024
TAILNET = ipaddress.IPv4Network("100.64.0.0/10")
# Tailscale on Windows may connect only when its tray app starts, which
# Windows can delay by minutes after logon (Owner decision: 3 minutes).
TAILSCALE_WAIT = 180
TAILSCALE_POLL = 5


class Hold(ValueError):
    pass


def _ordinary(path, *, directory=False):
    info = path.lstat()
    valid_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (not valid_type(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or (not directory and info.st_nlink != 1)):
        raise Hold(f"ordinary {'directory' if directory else 'file'} required: {path.name}")
    return info


def _log_paths():
    parent = Path(os.environ.get("LOCALAPPDATA", ""))
    if not parent.is_absolute() or str(parent).startswith(("\\\\", "//")) or ".." in parent.parts:
        raise Hold("LOCALAPPDATA must be an absolute local path")
    for directory in (*reversed(parent.parents), parent):
        _ordinary(directory, directory=True)
    folder = parent / "SoraCommanderNext"
    folder.mkdir(exist_ok=True)
    _ordinary(folder, directory=True)
    log = folder / LOG_NAME
    rotated = folder / (LOG_NAME + ".1")
    for path in (log, rotated):
        try:
            _ordinary(path)
        except FileNotFoundError:
            pass
    return log, rotated


def _pythonw(checkout):
    return checkout / ".venv" / "Scripts" / "pythonw.exe"


def task_xml(checkout, user, inbox):
    script = checkout / "tools" / "scn_muse_autostart.py"
    args = f'-I -B "{script}" run' + (" --inbox" if inbox else "")
    user = escape(user)
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Owner">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Owner">
    <Exec>
      <Command>{escape(str(_pythonw(checkout)))}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(str(checkout))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
'''


def _system_schtasks():
    # Name the system copy: a bare "schtasks" is looked up in the current
    # folder before System32 on Windows.
    root = Path(os.environ.get("SystemRoot", ""))
    if not root.is_absolute() or str(root).startswith(("\\\\", "//")):
        return None
    return str(root / "System32" / "schtasks.exe")


SCHTASKS = _system_schtasks()


def _schtasks(operation, *extra):
    if SCHTASKS is None:
        raise Hold("SystemRoot must be an absolute local path")
    return subprocess.run([SCHTASKS, operation, "/TN", TASK_NAME, *extra],
                          stdin=subprocess.DEVNULL,
                          capture_output=operation in ("/Query", "/End"))


def _task_exists():
    result = _schtasks("/Query")
    if result.returncode == 0:
        return True
    # A nonzero exit alone can also mean that the scheduler is inaccessible.
    if result.returncode == 1:
        missing = ctypes.FormatError(2).strip().casefold()  # ERROR_FILE_NOT_FOUND
        for encoding in ("oem", "mbcs", "utf-16-le"):
            message = result.stderr.decode(encoding, errors="replace").strip().casefold()
            if missing and message.endswith(missing):
                return False
    raise Hold(f"schtasks /Query exit code: {result.returncode}; task state could not be determined")


def _install(log, inbox):
    if not _pythonw(CHECKOUT).is_file():
        raise Hold("checkout .venv\\Scripts\\pythonw.exe is missing")
    domain = os.environ.get("USERDOMAIN", "")
    name = os.environ.get("USERNAME", "")
    if not domain.strip() or not name.strip():
        raise Hold("USERDOMAIN and USERNAME are required")
    if _task_exists():
        raise Hold("already installed; run the uninstaller first")

    xml = task_xml(CHECKOUT, f"{domain}\\{name}", inbox)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-16", newline="\n",
                                         dir=log.parent, suffix=".xml", delete=False) as file:
            temp = Path(file.name)
            file.write(xml)
        code = _schtasks("/Create", "/XML", str(temp)).returncode
        print(f"schtasks /Create exit code: {code}")
    finally:
        if temp is not None:
            temp.unlink()
    if code == 0:
        print(f'Start now with schtasks /Run /TN "{TASK_NAME}", or sign out and in.')
        print(f"Log: {log}")
        print("Remove with tools\\uninstall-muse-bridge-autostart.cmd.")
    return code


def _uninstall():
    if not _task_exists():
        print("Task is absent.")
        return 0
    _schtasks("/End")
    code = _schtasks("/Delete", "/F").returncode
    print(f"schtasks /Delete exit code: {code}")
    return code


def _address_is_local(address):
    # `tailscale ip -4` can report the remembered address before Windows has
    # assigned it to the Tailscale adapter; the bridge's bind then fails.
    # Bind an ephemeral port on it and close it at once (never port 8765).
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((address, 0))
    except OSError:
        return False
    return True


def _tailscale_address():
    executable = shutil.which("tailscale")
    if executable is None:
        program_files = Path(os.environ.get("ProgramFiles", ""))
        if program_files.is_absolute():
            executable = str(program_files / "Tailscale" / "tailscale.exe")

    start = time.monotonic()
    for attempt in range(TAILSCALE_WAIT // TAILSCALE_POLL):
        remaining = start + TAILSCALE_WAIT - time.monotonic()
        if remaining <= 0:
            break
        if executable is not None:
            try:
                result = subprocess.run(
                    [executable, "ip", "-4"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=min(5, remaining),
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                first, _, _ = result.stdout.partition("\n")
                if result.returncode == 0 and time.monotonic() <= start + TAILSCALE_WAIT:
                    address = ipaddress.IPv4Address(first.strip())
                    if address in TAILNET and _address_is_local(str(address)):
                        return str(address)
            except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
                pass
        delay = start + TAILSCALE_POLL * (attempt + 1) - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    raise Hold(f"Tailscale address not available after {TAILSCALE_WAIT} seconds")


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run(log, rotated, inbox):
    try:
        size = _ordinary(log).st_size
    except FileNotFoundError:
        size = 0
    if size > LOG_LIMIT:
        os.replace(log, rotated)
    with log.open("a", encoding="utf-8", buffering=1) as stream:
        with redirect_stdout(stream), redirect_stderr(stream):
            print(f"{_timestamp()} autostart start")
            try:
                address = _tailscale_address()
            except Hold as error:
                print(f"[HOLD] {error}")
                return 2
            except Exception as error:
                print(f"[ERROR] {type(error).__name__}")
                return 1
            try:
                sys.path.insert(0, str(CHECKOUT))
                from tools import scn_muse_bridge

                argv = ["--checkout", str(CHECKOUT), "--bind", address, "--port", "8765",
                        "--mode", "readonly", "--auth", "tailscale"]
                if inbox:
                    argv.append("--inbox")
                code = scn_muse_bridge.main(argv)
            except SystemExit as error:
                code = error.code if isinstance(error.code, int) else 1
                if isinstance(error.code, str):
                    print(f"[ERROR] {' '.join(error.code.splitlines())}")
            except Exception as error:
                print(f"[ERROR] {type(error).__name__}")
                code = 1
            print(f"{_timestamp()} bridge exit code: {code}")
            return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install").add_argument("--inbox", action="store_true")
    commands.add_parser("uninstall")
    commands.add_parser("run").add_argument("--inbox", action="store_true")
    args = parser.parse_args(argv)
    try:
        log, rotated = _log_paths()
        if args.command == "install":
            return _install(log, args.inbox)
        if args.command == "uninstall":
            return _uninstall()
        return _run(log, rotated, args.inbox)
    except (Hold, OSError) as error:
        if sys.stderr is not None:
            print(f"[HOLD] {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
