"""Windows Job Object real-descendant acceptance; disposable Python only.

No internet, service registration, inherited secrets, external jobs or project writes.
"""
import ctypes
from ctypes import wintypes
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.jobs import JobManager, LaunchOutcomeUnknown
from sora_commander_next.windows_job import WindowsJob

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_open = _kernel.OpenProcess
_open.restype = wintypes.HANDLE
_open.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_wait = _kernel.WaitForSingleObject
_wait.restype = wintypes.DWORD
_wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_close = _kernel.CloseHandle
_close.argtypes = (wintypes.HANDLE,)


def descendant_alive(pid):
    handle = _open(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists
            return False
        raise ctypes.WinError(error)  # Access denied is not absence evidence.
    try:
        result = _wait(handle, 0)
        if result == 0:  # WAIT_OBJECT_0
            return False
        if result == 0x00000102:  # WAIT_TIMEOUT
            return True
        raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _close(handle)


class DescendantObservationTests(unittest.TestCase):
    def test_missing_pid_is_absent(self):
        with patch(__name__ + "._open", return_value=None), patch.object(
                ctypes, "get_last_error", return_value=87):
            self.assertFalse(descendant_alive(123))

    def test_access_denied_is_unknown_not_absent(self):
        with patch(__name__ + "._open", return_value=None), patch.object(
                ctypes, "get_last_error", return_value=5):
            with self.assertRaises(OSError):
                descendant_alive(123)

    def test_wait_failure_is_unknown_and_handle_is_closed(self):
        with patch(__name__ + "._open", return_value=1), patch(
                __name__ + "._wait", return_value=0xffffffff), patch(
                    __name__ + "._close") as close, patch.object(
                        ctypes, "get_last_error", return_value=5):
            with self.assertRaises(OSError):
                descendant_alive(123)
            close.assert_called_once_with(1)

    def test_signaled_and_running_process_are_distinguished(self):
        for result, alive in ((0, False), (0x00000102, True)):
            with self.subTest(result=result), patch(
                    __name__ + "._open", return_value=1), patch(
                        __name__ + "._wait", return_value=result), patch(
                            __name__ + "._close"):
                self.assertEqual(descendant_alive(123), alive)


class WindowsJobTests(unittest.TestCase):
    def setUp(self):
        self.jobs = JobManager(max_concurrent=1)
        self.addCleanup(self.jobs.close)
        self.host = Host(jobs=self.jobs)
        self.pids = []

    def tearDown(self):
        self.jobs.close()
        for pid in self.pids:
            deadline = time.monotonic() + 2
            while descendant_alive(pid) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(descendant_alive(pid), f"descendant {pid} survived")

    def command(self, parent_exit=False, output_overflow=False, timeout=3, output_limit=4096):
        nested = "import time;time.sleep(15)"
        code = (
            "import subprocess,sys,time;"
            f"p=subprocess.Popen([sys.executable,'-I','-S','-c',{nested!r}],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL,close_fds=True);"
            "print('DESCENDANT_PID='+str(p.pid),flush=True);"
            + ("sys.stdout.write('x'*120000);sys.stdout.flush();" if output_overflow else "")
            + ("" if parent_exit else "time.sleep(15)")
        )
        return Command(sys.executable, ("-I", "-S", "-c", code), str(ROOT),
                       timeout=timeout, output_limit=output_limit)

    def start_with_descendant(self, request):
        started = self.jobs.start(request)
        session = started["session_id"]
        deadline = time.monotonic() + 2.5
        pid = None
        while time.monotonic() < deadline:
            result = self.jobs.read(session)
            match = re.search(r"DESCENDANT_PID=(\d+)", result["stdout"])
            if match:
                pid = int(match.group(1))
                break
            if result["status"] not in ("RUNNING", "CANCELLING"):
                break
            time.sleep(.015)
        self.assertIsNotNone(pid, result)
        self.pids.append(pid)
        return session, pid

    def test_cancel_reaps_grandchild_without_host_failure(self):
        sid, pid = self.start_with_descendant(self.command())
        self.assertTrue(descendant_alive(pid))
        self.assertEqual(self.jobs.stop(sid)["status"], "CANCELLING")
        result = self.jobs.wait(sid)
        self.assertEqual(result["status"], "CANCELLED", result)
        self.assertEqual(self.host.ping()["status"], "OK")

    def test_host_stop_wait_before_deadline_reaps_real_descendant(self):
        # Host/ChatGPT-facing stop path, not only direct JobManager.stop().
        # Real Windows Job Object, disposable isolated Python tree, no Owner UI.
        sid, pid = self.start_with_descendant(self.command(timeout=8))
        self.assertTrue(descendant_alive(pid))
        result = self.host.stop_process_wait(sid, wait_seconds=4)
        self.assertEqual(result["stop_request_status"], "CANCELLING", result)
        self.assertEqual(result["status"], "CANCELLED", result)
        self.assertEqual(result["exit_code"], 1)
        self.assertFalse(result["output_incomplete"], result)
        deadline = time.monotonic() + 2
        while descendant_alive(pid) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(descendant_alive(pid), "owned descendant survived a completed stop")
        self.assertEqual(self.host.ping()["status"], "OK")

    def test_host_stop_wait_after_terminal_timeout_preserves_history(self):
        sid, pid = self.start_with_descendant(self.command(timeout=.5))
        original = self.jobs.wait(sid)
        self.assertEqual(original["status"], "TIMEOUT", original)
        result = self.host.stop_process_wait(sid, wait_seconds=0)
        self.assertEqual(result["status"], "TIMEOUT", result)
        self.assertEqual(result["stop_request_status"], "TIMEOUT", result)
        self.assertEqual(result["session_id"], sid)
        self.assertEqual(result["captured_bytes"], original["captured_bytes"])
        self.assertFalse(descendant_alive(pid), "timed-out descendant survived")
        self.assertEqual(self.host.ping()["status"], "OK")

    def test_timeout_reaps_grandchild(self):
        sid, pid = self.start_with_descendant(self.command(timeout=.5))
        result = self.jobs.wait(sid)
        self.assertEqual(result["status"], "TIMEOUT", result)
        self.assertEqual(self.host.status()["status"], "OK")

    def test_direct_parent_exit_reaps_grandchild(self):
        sid, pid = self.start_with_descendant(self.command(parent_exit=True))
        result = self.jobs.wait(sid)
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(self.host.ping()["status"], "OK")

    def test_output_overflow_reaps_grandchild(self):
        sid, pid = self.start_with_descendant(self.command(output_overflow=True, output_limit=512))
        result = self.jobs.wait(sid)
        self.assertEqual(result["status"], "OUTPUT_LIMIT", result)
        self.assertTrue(result["overflow"])
        self.assertEqual(self.host.ping()["status"], "OK")

    def test_failed_assignment_never_resumes_unmanaged_child(self):
        children = []
        real_popen = subprocess.Popen
        def collect(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        with patch.object(WindowsJob, "attach_and_resume", side_effect=OSError("simulated assignment failure")):
            with patch("sora_commander_next.jobs.subprocess.Popen", side_effect=collect):
                # Launch classification (OUTCOME_UNKNOWN fix): a failure
                # during resume is UNKNOWN, not a plain OSError. The
                # child is still discarded and the host stays healthy,
                # as asserted below; only the exception type changed.
                with self.assertRaises(LaunchOutcomeUnknown) as caught:
                    self.jobs.start(Command(sys.executable, ("-I", "-S", "-c", "import time;time.sleep(15)"), str(ROOT)))
                self.assertEqual(caught.exception.phase, "resuming")
                self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(self.host.ping()["status"], "OK")


if __name__ == "__main__":
    unittest.main()
