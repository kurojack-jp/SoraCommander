"""Windows-only, fail-closed job tree containment.

A new child starts suspended, is assigned to a kill-on-close Job Object, and
only then has its primary thread resumed. No remote command can configure this.
Handles remain in the MCP host, not in the worker process.
"""

import ctypes
from ctypes import wintypes
import os
import time

if os.name != "nt":
    raise ImportError("Windows Job Objects require Windows")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_DWORD = wintypes.DWORD
_HANDLE = wintypes.HANDLE
_BOOL = wintypes.BOOL
_SIZE_T = ctypes.c_size_t


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", _SIZE_T),
        ("MaximumWorkingSetSize", _SIZE_T),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", _SIZE_T),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", _SIZE_T),
        ("JobMemoryLimit", _SIZE_T),
        ("PeakProcessMemoryUsed", _SIZE_T),
        ("PeakJobMemoryUsed", _SIZE_T),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", _DWORD), ("cntUsage", _DWORD),
        ("th32ThreadID", _DWORD), ("th32OwnerProcessID", _DWORD),
        ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", _DWORD),
    ]


def _api(name, restype, *args):
    func = getattr(_k32, name)
    func.restype = restype
    func.argtypes = args
    return func


_create_job = _api("CreateJobObjectW", _HANDLE, wintypes.LPVOID, wintypes.LPCWSTR)
_set_job_info = _api("SetInformationJobObject", _BOOL, _HANDLE, ctypes.c_int, wintypes.LPVOID, _DWORD)
_open_process = _api("OpenProcess", _HANDLE, _DWORD, _BOOL, _DWORD)
_assign = _api("AssignProcessToJobObject", _BOOL, _HANDLE, _HANDLE)
_terminate_job = _api("TerminateJobObject", _BOOL, _HANDLE, _DWORD)
_close = _api("CloseHandle", _BOOL, _HANDLE)
_snapshot_threads = _api("CreateToolhelp32Snapshot", _HANDLE, _DWORD, _DWORD)
_first_thread = _api("Thread32First", _BOOL, _HANDLE, ctypes.POINTER(_ThreadEntry))
_next_thread = _api("Thread32Next", _BOOL, _HANDLE, ctypes.POINTER(_ThreadEntry))
_open_thread = _api("OpenThread", _HANDLE, _DWORD, _BOOL, _DWORD)
_resume_thread = _api("ResumeThread", _DWORD, _HANDLE)


def _check(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def _primary_thread_id(pid):
    # CREATE_SUSPENDED prevents the initial thread from running/spawning children.
    for _ in range(5):
        snapshot = _snapshot_threads(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = _first_thread(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == pid:
                    return entry.th32ThreadID
                found = _next_thread(snapshot, ctypes.byref(entry))
        finally:
            _close(snapshot)
        time.sleep(0.01)
    raise RuntimeError("suspended thread unavailable")


class WindowsJob:
    """Owns a noninheritable kill-on-close job handle; close is idempotent."""

    def __init__(self):
        self._handle = _create_job(None, None)
        _check(self._handle)
        try:
            limits = _ExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            _check(_set_job_info(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def attach_and_resume(self, child):
        if self._handle is None:
            raise RuntimeError("job closed")
        process = _open_process(0x0100 | 0x0001, False, child.pid)  # SET_QUOTA | TERMINATE
        _check(process)
        try:
            _check(_assign(self._handle, process))
        finally:
            _close(process)
        tid = _primary_thread_id(child.pid)
        thread = _open_thread(0x0002, False, tid)  # THREAD_SUSPEND_RESUME
        _check(thread)
        try:
            if _resume_thread(thread) != 1:
                raise RuntimeError("unexpected suspended thread count")
        finally:
            _close(thread)

    def terminate(self):
        if self._handle is not None:
            _check(_terminate_job(self._handle, 1))

    def close(self):
        if self._handle is not None:
            handle, self._handle = self._handle, None
            _check(_close(handle))
