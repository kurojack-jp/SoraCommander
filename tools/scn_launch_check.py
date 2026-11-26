"""Read-only Windows launch checks for the checkout's batch launchers."""

import argparse
import ctypes
from ctypes import wintypes
import errno
import os
from pathlib import Path
import socket
import sys
import time

if __package__:
    from .scn_tunnel_client import checkout_path_hold
else:
    sys.path.insert(0, str(Path(__file__).absolute().parent))
    from scn_tunnel_client import checkout_path_hold


class ProcessEntry(ctypes.Structure):
    # PROCESSENTRY32W from tlhelp32.h. The heap ID is pointer-sized.
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


def tunnel_present():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for function in (api.Process32FirstW, api.Process32NextW):
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        function.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL

    snapshot = api.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not api.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        while True:
            if not entry.szExeFile:
                raise ValueError("empty process name")
            if entry.szExeFile.casefold() == "tunnel-client.exe":
                return True
            if not api.Process32NextW(snapshot, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error == 18:  # ERROR_NO_MORE_FILES
                    return False
                raise ctypes.WinError(error)
    finally:
        if not api.CloseHandle(snapshot):
            raise ctypes.WinError(ctypes.get_last_error())


def broker_present():
    names = os.listdir("\\\\.\\pipe\\")
    if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
        raise ValueError("invalid pipe listing")
    return any(name.casefold().startswith("soracommandernext-broker-") for name in names)


def wait_broker(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if broker_present():
            return True
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.5, remaining))
    return False


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind((socket.inet_ntoa(bytes((127, 0, 0, 1))), port))
        except OSError as error:
            # An existing exclusive bind can produce WSAEACCES or WSAEADDRINUSE.
            if error.errno in (errno.EADDRINUSE, 10048, 10013):
                return False
            raise
    return True


class TcpRow4(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in
                ("state", "local_addr", "local_port", "remote_addr", "remote_port", "pid")]


class TcpRow6(ctypes.Structure):
    _fields_ = [
        ("local_addr", ctypes.c_ubyte * 16), ("local_scope", wintypes.DWORD),
        ("local_port", wintypes.DWORD), ("remote_addr", ctypes.c_ubyte * 16),
        ("remote_scope", wintypes.DWORD), ("remote_port", wintypes.DWORD),
        ("state", wintypes.DWORD), ("pid", wintypes.DWORD),
    ]


def _family_listening(query, family, row_type, port):
    buffer = None
    size = wintypes.DWORD()
    while True:
        status = query(buffer, ctypes.byref(size), False, family, 3, 0)  # TCP_TABLE_OWNER_PID_LISTENER
        if status != 122:  # ERROR_INSUFFICIENT_BUFFER
            break
        if size.value <= (len(buffer) if buffer is not None else 0):
            raise ValueError("invalid TCP table size")
        buffer = ctypes.create_string_buffer(size.value)
    if status:
        raise ctypes.WinError(status)
    header = ctypes.sizeof(wintypes.DWORD)
    if buffer is None or not header <= size.value <= len(buffer):
        raise ValueError("invalid TCP table header")
    count = wintypes.DWORD.from_buffer(buffer).value
    if count > (size.value - header) // ctypes.sizeof(row_type):
        raise ValueError("truncated TCP table")
    rows = (row_type * count).from_buffer(buffer, header)
    return any(row.state == 2 and socket.ntohs(row.local_port & 0xffff) == port for row in rows)


def port_listening(port):
    query = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                      wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    query.restype = wintypes.DWORD
    ipv4 = _family_listening(query, socket.AF_INET, TcpRow4, port)
    ipv6 = _family_listening(query, socket.AF_INET6, TcpRow6, port)
    return ipv4 or ipv6


def check_allowed_roots(checkout):
    sys.path.insert(0, str(checkout / "src"))
    try:
        from sora_commander_next.allowed_roots import (
            AllowedRootsError, MISSING_FOLDER, startup_allowed_roots)
        try:
            roots = startup_allowed_roots(checkout, "development")
        except AllowedRootsError as error:
            print(f"{error.line}: {error.reason}")
            return 9
    except (ImportError, OSError, ValueError, RuntimeError):
        print("0: configuration unavailable or invalid")
        return 9
    finally:
        sys.path.pop(0)
    for line in roots.skipped:
        print(f"{line}: {MISSING_FOLDER}")
    return 8 if roots.skipped else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--tunnel-absent", action="store_true")
    parser.add_argument("--broker-absent", action="store_true")
    parser.add_argument("--wait-broker", type=int, metavar="SECONDS")
    parser.add_argument("--port-free", type=int, metavar="PORT")
    parser.add_argument("--port-listening", type=int, metavar="PORT")
    parser.add_argument("--allowed-roots", action="store_true",
                        help="check allowed folders (0 valid, 8 skipped, 9 refused)")
    args = parser.parse_args(argv)
    if args.wait_broker is not None and not 1 <= args.wait_broker <= 120:
        parser.error("SECONDS must be between 1 and 120")
    if any(port is not None and not 1 <= port <= 65535 for port in (args.port_free, args.port_listening)):
        parser.error("PORT must be between 1 and 65535")
    if not any(vars(args).values()):
        parser.error("select at least one launch check")
    checkout = Path(__file__).absolute().parents[1]
    if args.allowed_roots:
        if any(value for key, value in vars(args).items() if key != "allowed_roots"):
            parser.error("--allowed-roots must be used by itself")
        return check_allowed_roots(checkout)
    if message := checkout_path_hold(checkout):
        print(f"[HOLD] {message}")
        return 6

    for enabled, description, query, arguments, blocked, code in (
        (args.tunnel_absent, "tunnel processes", tunnel_present, (), True, 3),
        (args.broker_absent, "Broker pipes", broker_present, (), True, 5),
        (args.wait_broker is not None, "Broker pipes", wait_broker, (args.wait_broker,), False, 4),
        (args.port_free is not None, "the loopback port", port_free, (args.port_free,), False, 3),
        (args.port_listening is not None, "TCP listeners", port_listening, (args.port_listening,), True, 3),
    ):
        if enabled:
            try:
                result = query(*arguments)
                if type(result) is not bool:
                    raise ValueError("invalid query result")
            except Exception as error:
                winerror = getattr(error, "winerror", None)
                detail = f" [WinError {winerror}]" if isinstance(winerror, int) else ""
                print(f"[HOLD] Could not check {description}.{detail}")
                return 7
            if result is blocked:
                return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
