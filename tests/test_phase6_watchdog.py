"""Local disposable Host crash/restart tests; no Task, port, service or tunnel."""
from pathlib import Path
import ctypes
from ctypes import wintypes
import os
import signal
import tempfile
import sys
from threading import Thread
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.host_supervisor import HostSupervisor


class Phase6WatchdogTests(unittest.TestCase):
    def make(self, code="import time;time.sleep(15)", max_restarts=2):
        return HostSupervisor(
            [sys.executable, "-I", "-S", "-c", code], str(ROOT),
            max_restarts=max_restarts, poll_seconds=.02, backoff_seconds=.02
        )

    def start(self, watcher):
        thread = Thread(target=watcher.run, daemon=True)
        thread.start()
        self.addCleanup(self.cleanup, watcher, thread)
        return thread

    @staticmethod
    def cleanup(watcher, thread):
        watcher.stop()
        thread.join(timeout=3)
        if thread.is_alive():
            raise AssertionError("watchdog did not shut down")

    @staticmethod
    def until(watcher, predicate, seconds=2):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            status = watcher.snapshot()
            if predicate(status):
                return status
            time.sleep(.01)
        raise AssertionError(f"watchdog state not reached: {watcher.snapshot()}")

    def test_killed_host_is_restarted_once_without_replaying_requests(self):
        watcher = self.make(max_restarts=1)
        thread = self.start(watcher)
        first = self.until(watcher, lambda s: s["state"] == "RUNNING" and s["host_pid"] is not None)
        oldpid = first["host_pid"]
        os.kill(oldpid, signal.SIGTERM)
        new = self.until(watcher, lambda s: s["generation"] == 2 and s["host_pid"] is not None)
        self.assertNotEqual(oldpid, new["host_pid"])
        self.assertEqual(new["restarts"], 1)
        self.assertFalse(new["replay"])
        self.assertFalse(new["remote_ready"])
        watcher.stop()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(watcher.snapshot()["state"], "STOPPED")

    def test_bounded_crashes_do_not_spin_forever(self):
        watcher = self.make("import sys;sys.exit(3)", max_restarts=1)
        thread = self.start(watcher)
        status = self.until(watcher, lambda s: s["state"] == "CRASH_LIMIT")
        self.assertEqual(status["generation"], 2)
        self.assertEqual(status["restarts"], 1)
        self.assertEqual(status["last_host_exit_code"], 3)
        self.assertIsNone(status["host_pid"])
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_clean_exit_does_not_restart_and_single_start_only(self):
        watcher = self.make("import sys;sys.exit(0)")
        thread = self.start(watcher)
        status = self.until(watcher, lambda s: s["state"] == "CLEAN_EXIT")
        self.assertEqual(status["generation"], 1)
        self.assertEqual(status["restarts"], 0)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        with self.assertRaises(RuntimeError):
            watcher.run()

    @staticmethod
    def is_running(pid):
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.restype = wintypes.HANDLE
        api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        handle = api.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return api.WaitForSingleObject(handle, 0) == 0x00000102
        finally:
            api.CloseHandle(handle)

    def test_crashed_host_reaps_original_grandchild_and_starts_fresh(self):
        with tempfile.TemporaryDirectory(prefix="sora-phase6-") as directory:
            folder = Path(directory)
            marker = folder / "once.txt"
            descendant = folder / "descendant.txt"
            fixture = ROOT / "tests" / "fixtures" / "watchdog_crash_host.py"
            watcher = HostSupervisor(
                [sys.executable, "-I", "-S", str(fixture), str(marker), str(descendant)],
                str(ROOT), max_restarts=1, poll_seconds=.02, backoff_seconds=.02
            )
            thread = self.start(watcher)
            first = self.until(watcher, lambda s: s["state"] == "RUNNING" and s["host_pid"])
            end = time.monotonic() + 3
            while not descendant.exists() and time.monotonic() < end:
                time.sleep(.02)
            self.assertTrue(descendant.exists())
            grandchild_pid = int(descendant.read_text(encoding="utf-8"))
            self.assertTrue(self.is_running(grandchild_pid))
            os.kill(first["host_pid"], signal.SIGTERM)
            try:
                second = self.until(watcher, lambda s: s["generation"] == 2 and s["host_pid"])
                self.assertNotEqual(first["host_pid"], second["host_pid"])
                end = time.monotonic() + 3
                while (not (folder / "restarted.txt").exists() or self.is_running(grandchild_pid)) and time.monotonic() < end:
                    time.sleep(.02)
                self.assertFalse(self.is_running(grandchild_pid))
                self.assertEqual(marker.read_text(encoding="utf-8"), "once")
                self.assertEqual((folder / "restarted.txt").read_text(encoding="utf-8"), "fresh")
                self.assertFalse(second["replay"])
            finally:
                watcher.stop()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())

    def test_configuration_rejects_implicit_or_unbounded_launch(self):
        for argv, cwd, restarts in [
            (["python", "--version"], str(ROOT), 1),
            ([], str(ROOT), 1),
            ([sys.executable], "relative", 1),
            ([sys.executable], str(ROOT), 100),
        ]:
            with self.subTest(argv=argv, cwd=cwd), self.assertRaises(ValueError):
                HostSupervisor(argv, cwd, max_restarts=restarts)


if __name__ == "__main__":
    unittest.main()
