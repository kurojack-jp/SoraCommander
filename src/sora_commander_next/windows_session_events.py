"""Opt-in, process-local Windows WTS session event invalidation latch.

A private hidden Win32 window runs its own message pump on a local thread.
Any WTS session-change notification for the pinned Owner session irrevocably
invalidates this *one review*. No change to Task, OS lock, window focus, or
global session configuration. Not a replacement for Windows token/lock checks.
"""
import ctypes
from ctypes import wintypes
import os
from threading import Event, Lock, Thread
from uuid import uuid4

if os.name != "nt":
    raise ImportError("Windows session event latch requires Windows")

WM_WTSSESSION_CHANGE = 0x02B1
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
NOTIFY_FOR_THIS_SESSION = 0

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
wts = ctypes.WinDLL("wtsapi32", use_last_error=True)

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                            wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


def winapi(lib, name, result, *arguments):
    func = getattr(lib, name)
    func.restype = result
    func.argtypes = arguments
    return func


get_module = winapi(kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
register_class = winapi(user32, "RegisterClassW", wintypes.ATOM,
                        ctypes.POINTER(WNDCLASSW))
unregister_class = winapi(user32, "UnregisterClassW", wintypes.BOOL,
                          wintypes.LPCWSTR, wintypes.HINSTANCE)
create_window = winapi(user32, "CreateWindowExW", wintypes.HWND,
                       wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                       wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, wintypes.HWND,
                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
destroy_window = winapi(user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
default_proc = winapi(user32, "DefWindowProcW", ctypes.c_ssize_t,
                      wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
get_message = winapi(user32, "GetMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG),
                     wintypes.HWND, wintypes.UINT, wintypes.UINT)
dispatch_message = winapi(user32, "DispatchMessageW", ctypes.c_ssize_t,
                          ctypes.POINTER(wintypes.MSG))
post_message = winapi(user32, "PostMessageW", wintypes.BOOL,
                      wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
post_quit = winapi(user32, "PostQuitMessage", None, ctypes.c_int)
wts_register = winapi(wts, "WTSRegisterSessionNotification", wintypes.BOOL,
                      wintypes.HWND, wintypes.DWORD)
wts_unregister = winapi(wts, "WTSUnRegisterSessionNotification", wintypes.BOOL,
                        wintypes.HWND)


class OwnerSessionEventLatch:
    """One explicit review; never cleared after any target session event."""

    def __init__(self, session_id):
        if type(session_id) is not int or not 0 < session_id < 0xFFFFFFFF:
            raise ValueError("positive Windows Owner session required")
        self._session_id = session_id
        self._lock = Lock()
        self._ready = Event()
        self._invalidated = Event()
        self._session_events = set()
        self._thread = None
        self._hwnd = None
        self._state = "NOT_STARTED"

    def start(self):
        with self._lock:
            if self._state != "NOT_STARTED":
                raise RuntimeError("one-shot session latch")
            self._state = "STARTING"
            self._thread = Thread(target=self._run, daemon=True,
                                  name="sora-owner-session-latch")
            self._thread.start()
        if not self._ready.wait(3):
            self._invalidated.set()
            raise PermissionError("Windows session listener startup timeout")
        self.verify()
        return self

    def verify(self):
        with self._lock:
            healthy = self._state == "READY" and self._thread is not None and self._thread.is_alive()
        if not healthy or self._invalidated.is_set():
            raise PermissionError("Owner session change invalidated this review")
        return True

    def invalidated(self):
        return self._invalidated.is_set()

    def saw_lock_event(self):
        """Diagnostic only; no identity, presence or click attestation."""
        with self._lock:
            return 0x7 in self._session_events  # WTS_SESSION_LOCK

    def close(self):
        with self._lock:
            if self._state in ("NOT_STARTED", "CLOSED"):
                self._state = "CLOSED"
                self._invalidated.set()
                return
            self._invalidated.set()
            hwnd, thread = self._hwnd, self._thread
        if hwnd:
            if not post_message(hwnd, WM_CLOSE, 0, 0):
                # Cannot guarantee listener shutdown: do not approve.
                raise PermissionError("Owner session listener stop failed")
        if thread:
            thread.join(timeout=2)
            if thread.is_alive():
                raise PermissionError("Owner session listener failed to exit")
        with self._lock:
            self._state = "CLOSED"

    def _run(self):
        hwnd = None
        registered = False
        atom = None
        hinstance = None
        class_name = "SoraOwnerWTS_" + uuid4().hex
        try:
            hinstance = get_module(None)
            if not hinstance:
                raise RuntimeError("missing local module")
            @WNDPROC
            def callback(window, msg, wparam, lparam):
                try:
                    if msg == WM_WTSSESSION_CHANGE:
                        if int(lparam) == self._session_id:
                            # Conservative: ANY change invalidates an open
                            # review, including reconnect/unlock, permanently.
                            with self._lock:
                                self._session_events.add(int(wparam))
                            self._invalidated.set()
                        return 0
                    if msg == WM_CLOSE:
                        # The WTS registration must be removed BEFORE the
                        # window is destroyed (cleanup belongs in finally).
                        post_quit(0)
                        return 0
                    return default_proc(window, msg, wparam, lparam)
                except BaseException:
                    self._invalidated.set()
                    return 0
            clazz = WNDCLASSW()
            clazz.lpfnWndProc = callback
            clazz.hInstance = hinstance
            clazz.lpszClassName = class_name
            atom = register_class(ctypes.byref(clazz))
            if not atom:
                raise RuntimeError("failed to register local WTS window class")
            # Hidden top-level window. It is never shown or activated.
            hwnd = create_window(0, class_name, "SoraOwnerLocalSessionWatcher",
                                 0, 0, 0, 0, 0, None, None, hinstance, None)
            if not hwnd:
                raise RuntimeError("failed to create hidden WTS window")
            if not wts_register(hwnd, NOTIFY_FOR_THIS_SESSION):
                raise RuntimeError("WTS session event registration unavailable")
            registered = True
            with self._lock:
                self._hwnd = hwnd
                self._state = "READY"
            self._ready.set()
            msg = wintypes.MSG()
            while True:
                value = get_message(ctypes.byref(msg), None, 0, 0)
                if value <= 0:
                    if value < 0:
                        self._invalidated.set()
                    break
                dispatch_message(ctypes.byref(msg))
        except BaseException:
            self._invalidated.set()
        finally:
            self._invalidated.set()
            if registered:
                if not wts_unregister(hwnd):
                    self._invalidated.set()
            if hwnd:
                destroy_window(hwnd)
            if atom and hinstance:
                unregister_class(class_name, hinstance)
            with self._lock:
                self._hwnd = None
                if self._state != "CLOSED":
                    self._state = "STOPPED"
            self._ready.set()
