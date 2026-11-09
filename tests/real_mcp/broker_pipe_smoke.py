"""Opt-in ephemeral local IPC smoke: no service, task, CLI, or MCP changes."""
import ctypes
from ctypes import wintypes
import json
import os
import sys
import threading
import time
import uuid

from sora_commander_next.windows_owner import OwnerSessionGuard
from sora_commander_next.windows_pipe_listener import serve_once
from sora_commander_next.broker_dispatch import BrokerDispatcher
from sora_commander_next.broker_frames import pack_frame, read_frame

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
open_pipe = kernel.CreateFileW
open_pipe.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
open_pipe.restype = wintypes.HANDLE
read = kernel.ReadFile
read.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
read.restype = wintypes.BOOL
write = kernel.WriteFile
write.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
write.restype = wintypes.BOOL
close = kernel.CloseHandle
close.argtypes = [wintypes.HANDLE]
close.restype = wintypes.BOOL

def client(name):
    handle = open_pipe(name, 0xC0000000, 0, None, 3, 0, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError("CreateFileW failed: " + str(ctypes.get_last_error()))
    try:
        payload = pack_frame(b'{"version":1,"op":"health"}')
        count = wintypes.DWORD()
        if not write(handle, payload, len(payload), ctypes.byref(count), None) or count.value != len(payload):
            raise OSError("write failed")
        def exact(n):
            buf = ctypes.create_string_buffer(n)
            got = wintypes.DWORD()
            if not read(handle, buf, n, ctypes.byref(got), None):
                raise OSError("read failed: " + str(ctypes.get_last_error()))
            return buf.raw[:got.value]
        return json.loads(read_frame(exact))
    finally:
        close(handle)

def main():
    guard = OwnerSessionGuard()
    name = r"\\.\pipe\SoraCommanderNext-smoke-" + uuid.uuid4().hex
    dispatch = BrokerDispatcher(owner_guard=guard, lookup=lambda _: None, cancel=lambda _: False)
    result = []
    def server():
        try:
            serve_once(owner_guard=guard, dispatcher=dispatch, pipe_name=name)
            result.append("SERVER_OK")
        except Exception as e:
            result.append(type(e).__name__ + ": " + str(e))
    t = threading.Thread(target=server, daemon=True)
    t.start()
    response = None
    errors = []
    for _ in range(30):
        try:
            response = client(name)
            break
        except OSError as e:
            errors.append(str(e))
            time.sleep(0.1)
    t.join(3)
    print("RESPONSE=" + repr(response), flush=True)
    print("SERVER=" + repr(result), flush=True)
    if response != {"version": 1, "status": "OK", "request_id": None} or result != ["SERVER_OK"]:
        print("SMOKE_FAIL last_client=" + repr(errors[-1:] if errors else []), flush=True)
        return 1
    print("SMOKE_PASS", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
