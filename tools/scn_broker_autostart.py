"""Install, remove, or run the current user's Broker mode logon task."""

import argparse
import ctypes
from ipaddress import IPv4Address
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
from xml.sax.saxutils import escape


CHECKOUT = Path(__file__).absolute().parents[1]
# The task uses -I, which omits the checkout from the import path.
if __name__ == "__main__":
    sys.path.insert(0, str(CHECKOUT))

from tools.scn_muse_autostart import Hold, LOG_LIMIT, _ordinary, _pythonw, _timestamp
from tools.scn_tunnel_client import checkout_path_hold


TASK_NAME = "SoraCommander Broker mode"
LOG_NAME = "broker-autostart.log"
NETWORK_WAIT = 180
NETWORK_POLL = 5
ROUTE_ABSENT = 3  # Distinct from interpreter and WMI errors.
BROKER_WAIT = 30
TUNNEL_WAIT = 30
HEALTH_ADDRESS = (str(IPv4Address(0x7F000001)), 8080)
BROKER_READY = b"[READY] Owner Broker is running. DEV only. Press Ctrl+C here to stop it."
BROKER_LAUNCHER = r"tools\start-owner-broker-dev.cmd"
TUNNEL_LAUNCHER = r"tools\start-sora-commander-broker-dev.cmd"
CREATE_SUSPENDED = 0x00000004


def _log_paths():
    parent = Path(os.environ.get("LOCALAPPDATA", ""))
    if not parent.is_absolute() or str(parent).startswith(("\\\\", "//")) or ".." in parent.parts:
        raise Hold("LOCALAPPDATA must be an absolute local path")
    for directory in (*reversed(parent.parents), parent):
        _ordinary(directory, directory=True)
    folder = parent / "SoraCommanderNext"
    folder.mkdir(exist_ok=True)
    _ordinary(folder, directory=True)
    log, rotated = folder / LOG_NAME, folder / (LOG_NAME + ".1")
    for path in (log, rotated):
        try:
            _ordinary(path)
        except FileNotFoundError:
            pass
    return log, rotated


def task_xml(checkout, user):
    script = checkout / "tools" / "scn_broker_autostart.py"
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
      <Arguments>{escape(f'-I -B "{script}" run')}</Arguments>
      <WorkingDirectory>{escape(str(checkout))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
'''


def _system_binary(relative):
    root = Path(os.environ.get("SystemRoot", ""))
    if not root.is_absolute() or str(root).startswith(("\\\\", "//")) or ".." in root.parts:
        raise Hold("SystemRoot must be an absolute local path")
    return str(root / "System32" / relative)


def _schtasks(operation, *extra):
    return subprocess.run(
        [_system_binary("schtasks.exe"), operation, "/TN", TASK_NAME, *extra],
        stdin=subprocess.DEVNULL, capture_output=operation in ("/Query", "/End"),
    )


def _task_exists():
    result = _schtasks("/Query")
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        missing = ctypes.FormatError(2).strip().casefold()  # ERROR_FILE_NOT_FOUND
        for encoding in ("oem", "mbcs", "utf-16-le"):
            message = result.stderr.decode(encoding, errors="replace").strip().casefold()
            if missing and message.endswith(missing):
                return False
    raise Hold(f"schtasks /Query exit code: {result.returncode}; task state could not be determined")


def _install(log):
    if not _pythonw(CHECKOUT).is_file():
        raise Hold("checkout .venv\\Scripts\\pythonw.exe is missing")
    domain = os.environ.get("USERDOMAIN", "")
    name = os.environ.get("USERNAME", "")
    if not domain.strip() or not name.strip():
        raise Hold("USERDOMAIN and USERNAME are required")
    if _task_exists():
        raise Hold("already installed; run the uninstaller first")

    xml = task_xml(CHECKOUT, f"{domain}\\{name}")
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
        print(r"Remove with tools\uninstall-broker-autostart.cmd.")
    return code


def _uninstall():
    if not _task_exists():
        print("Task is absent.")
        return 0
    _schtasks("/End")
    code = _schtasks("/Delete", "/F").returncode
    print(f"schtasks /Delete exit code: {code}")
    return code


def _connected_default_route():
    from win32com.client import GetObject

    wmi = GetObject(r"winmgmts:\\.\root\StandardCimv2")
    routes = wmi.ExecQuery("SELECT InterfaceIndex, AddressFamily FROM MSFT_NetRoute "
                           "WHERE Store = 1 AND DestinationPrefix LIKE '%/0'")
    interfaces = wmi.ExecQuery("SELECT InterfaceIndex, AddressFamily FROM MSFT_NetIPInterface "
                               "WHERE ConnectionState = 1")

    def key(row):
        index = row.Properties_("InterfaceIndex").Value
        family = row.Properties_("AddressFamily").Value
        if type(index) is not int or index <= 0 or type(family) is not int or family not in (2, 23):
            raise ValueError("Invalid Windows IP interface")
        return index, family

    return not {key(row) for row in routes}.isdisjoint({key(row) for row in interfaces})


def _route_probe_exit():
    try:
        return 0 if _connected_default_route() else ROUTE_ABSENT
    except Exception:
        return 2


def _wait_for_network():
    # Each probe is a child with its own timeout, so a blocked WMI provider
    # cannot extend the logon deadline. WMI may still be starting at logon:
    # a failed query is retried until the deadline, then reported as HOLD.
    deadline = time.monotonic() + NETWORK_WAIT
    query_failed = False
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(Path(__file__).absolute()), "--query-route"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=min(NETWORK_POLL, remaining),
            )
            if result.returncode == 0 and time.monotonic() < deadline:
                return
            query_failed = result.returncode not in (0, ROUTE_ABSENT)
        except subprocess.TimeoutExpired:
            query_failed = False
        except OSError as error:
            raise Hold("Could not inspect the Windows network route") from error
        delay = min(NETWORK_POLL, deadline - time.monotonic())
        if delay > 0:
            time.sleep(delay)
    if query_failed:
        raise Hold(f"Could not inspect the Windows network route for {NETWORK_WAIT} seconds")
    raise Hold(f"Network route not available after {NETWORK_WAIT} seconds")


def _start(job, launcher, *, stdin, stdout):
    child = subprocess.Popen(
        [_system_binary("cmd.exe"), "/d", "/c", launcher],
        cwd=CHECKOUT, stdin=stdin, stdout=stdout, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | CREATE_SUSPENDED,
    )
    try:
        job.attach_and_resume(child)
    except (OSError, RuntimeError):
        # The child may still be suspended outside the job if attachment failed.
        child.kill()
        child.wait()
        for stream in (child.stdin, child.stdout):
            if stream is not None:
                stream.close()
        raise
    return child


def _observe_broker(output, ready, finishing):
    try:
        for raw in iter(lambda: output.readline(4096), b""):
            line = raw.rstrip(b"\r\n")
            if line == BROKER_READY:
                ready.set()
            elif (line.startswith(b"[INFO] Broker finished with code ")
                  or (not ready.is_set() and line.startswith(b"[HOLD]"))):
                # Release the launcher's pause while preserving Broker exit status.
                finishing.set()
    finally:
        finishing.set()


def _broker_exit(code, log):
    if code == 6:
        print("[INFO] Owner journal is already in use. Nothing else was started.", file=log)
        return 0
    print(f"{_timestamp()} Broker exit code: {code}", file=log)
    return code


def _tunnel_ready(timeout):
    try:
        with socket.create_connection(HEALTH_ADDRESS, timeout=timeout):
            return True
    except OSError:
        return False


def _start_mode(log):
    sys.path.insert(0, str(CHECKOUT / "src"))
    from sora_commander_next.windows_job import WindowsJob

    job = WindowsJob()
    broker = None
    observer = None
    try:
        broker = _start(job, BROKER_LAUNCHER, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        ready, finishing = Event(), Event()
        observer = Thread(target=_observe_broker, args=(broker.stdout, ready, finishing), daemon=True)
        observer.start()
        deadline = time.monotonic() + BROKER_WAIT
        while not ready.is_set():
            if finishing.is_set():
                broker.stdin.close()
            code = broker.poll()
            if code is not None:
                if code == 0:
                    raise Hold("Broker exited before its pipe was ready")
                return _broker_exit(code, log)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Hold(f"Broker pipe not ready after {BROKER_WAIT} seconds")
            ready.wait(min(0.25, remaining))

        code = broker.poll()
        if finishing.is_set() or code is not None:
            broker.stdin.close()
            return _broker_exit(broker.wait(timeout=5), log)
        print("[INFO] Broker ready. Starting the tunnel once.", file=log)
        tunnel = _start(job, TUNNEL_LAUNCHER, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        deadline = time.monotonic() + TUNNEL_WAIT
        tunnel_ready = False
        while True:
            if finishing.is_set():
                broker.stdin.close()
            if (code := broker.poll()) is not None:
                return _broker_exit(code, log)
            remaining = deadline - time.monotonic()
            if not tunnel_ready and remaining <= 0:
                raise Hold(f"Tunnel health port not ready after {TUNNEL_WAIT} seconds")
            try:
                code = tunnel.wait(timeout=0.25 if tunnel_ready else min(0.25, remaining))
            except subprocess.TimeoutExpired:
                if not tunnel_ready and (remaining := deadline - time.monotonic()) > 0:
                    tunnel_ready = (_tunnel_ready(min(0.25, remaining))
                                    and time.monotonic() < deadline)
                continue
            print(f"{_timestamp()} Tunnel exit code: {code}", file=log)
            return code
    finally:
        try:
            if broker is not None:
                broker.stdin.close()
                try:
                    broker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            # Closing the job also stops its children when Scheduler ends this task.
            job.close()
            if observer is not None:
                observer.join()
            if broker is not None:
                broker.stdout.close()


def _run(log, rotated):
    try:
        size = _ordinary(log).st_size
    except FileNotFoundError:
        size = 0
    if size > LOG_LIMIT:
        os.replace(log, rotated)
    with log.open("a", encoding="utf-8", buffering=1) as stream:
        print(f"{_timestamp()} autostart start", file=stream)
        try:
            _wait_for_network()
            return _start_mode(stream)
        except Hold as error:
            print(f"[HOLD] {error}", file=stream)
            return 2
        except Exception as error:
            print(f"[ERROR] {type(error).__name__}", file=stream)
            return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "uninstall", "run"):
        commands.add_parser(name)
    args = parser.parse_args(argv)
    if args.command != "uninstall" and (message := checkout_path_hold(CHECKOUT)):
        if sys.stderr is not None:
            print(f"[HOLD] {message}", file=sys.stderr)
        return 6
    try:
        if args.command == "uninstall":
            return _uninstall()
        log, rotated = _log_paths()
        if args.command == "install":
            return _install(log)
        return _run(log, rotated)
    except (Hold, OSError) as error:
        message = type(error).__name__ if args.command == "run" else str(error)
        if sys.stderr is not None:
            print(f"[HOLD] {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if sys.argv[1:] == ["--query-route"]:
        raise SystemExit(_route_probe_exit())
    raise SystemExit(main())
