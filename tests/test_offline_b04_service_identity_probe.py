"""B04-a mock Win32 only: no native DLL calls, temp fixtures or children."""
import ast
import ctypes
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sora_commander_next import offline_b04_service_identity_probe as probe
from sora_commander_next.release_inventory import (
    DEV_ONLY_MODULES, MODULES, OPTIONAL_DEV_ONLY_MODULES)


class FakeWin32:
    """Raw ABI seam, not a synthetic asserted service identity adapter."""

    def __init__(self):
        self.events = []
        self.counts = {}
        self.change = {}
        self.error = 5
        self.adv = SimpleNamespace()
        self.kernel = SimpleNamespace()
        for dll, names in (
            (self.adv, ("OpenSCManagerW", "OpenServiceW", "QueryServiceStatusEx",
                        "CloseServiceHandle")),
            (self.kernel, ("OpenProcess", "GetProcessId", "GetProcessTimes",
                           "WaitForSingleObject", "CloseHandle")),
        ):
            for name in names:
                def invoke(*args, name=name):
                    self.events.append((name, args))
                    n = self.counts.get(name, 0) + 1
                    self.counts[name] = n
                    override = self.change.get((name, n))
                    if isinstance(override, BaseException):
                        raise override
                    if callable(override):
                        return override(*args)
                    if (name, n) in self.change:
                        return override
                    return getattr(self, name)(*args)
                setattr(dll, name, Mock(side_effect=invoke))
        self.api = probe._NativeCalls(self.adv, self.kernel, lambda: self.error)

    def OpenSCManagerW(self, machine, database, access):
        assert (machine, database, access) == (None, None, 1)
        return 101

    def OpenServiceW(self, scm, name, access):
        assert (scm, name, access) == (101, probe.SERVICE_NAME, 4)
        return 102

    def QueryServiceStatusEx(self, handle, level, buf, size, needed):
        assert (handle, level, size) == (102, 0, 36)
        data = ctypes.cast(buf, ctypes.POINTER(probe.SERVICE_STATUS_PROCESS)).contents
        data.dwServiceType = 0x10
        data.dwCurrentState = 4
        data.dwProcessId = 500
        # Success does not promise pcbBytesNeeded. Leave it untouched.
        return 1

    def OpenProcess(self, access, inherit, pid):
        assert (access, inherit, pid) == (0x101000, 0, 500)
        return 103

    def GetProcessId(self, handle):
        assert handle == 103
        return 500

    def GetProcessTimes(self, handle, created, exited, kernel, user):
        assert handle == 103
        value = ctypes.cast(created, ctypes.POINTER(probe.FILETIME)).contents
        value.dwHighDateTime = 0x01234567
        value.dwLowDateTime = 0x89ABCDEF
        return 1

    def WaitForSingleObject(self, handle, timeout):
        assert (handle, timeout) == (103, 0)
        return 258

    def CloseHandle(self, handle):
        assert handle == 103
        return 1

    def CloseServiceHandle(self, handle):
        assert handle in (101, 102)
        return 1

    def closed(self):
        return [(name, args[0]) for name, args in self.events if name.startswith("Close")]


class NativeObservationTests(unittest.TestCase):
    def setUp(self):
        # Fail immediately if any test accidentally reaches an actual DLL loader
        # or a process-launch route. Tests bind *every* Win32 call to FakeWin32.
        for target in ("ctypes.WinDLL", "subprocess.Popen", "os.system"):
            guard = patch(target, side_effect=AssertionError("real OS operation forbidden"),
                          create=True)
            guard.start()
            self.addCleanup(guard.stop)
        latch = patch.object(probe, "_cleanup_uncertain", False)
        latch.start()
        self.addCleanup(latch.stop)
        self.fake = FakeWin32()

    def observe(self, **kwargs):
        return probe.observe_service_process(_calls=self.fake.api, **kwargs)

    def rejected(self, code=None):
        with self.assertRaises(probe.ObservationError) as result:
            self.observe()
        if code:
            self.assertEqual(result.exception.code, code)
        self.assertIn("NOT_AUTHENTICATED", str(result.exception))
        return result.exception

    def test_snapshot_is_explicitly_unverified_and_has_no_authority_interface(self):
        result = self.observe()
        self.assertEqual((result.pid, result.created_filetime),
                         (500, 0x0123456789ABCDEF))
        self.assertEqual(result.service_name, probe.SERVICE_NAME)
        self.assertEqual(result.authentication, "NOT_AUTHENTICATED")
        self.assertEqual(result.status, "SCM_PROCESS_SNAPSHOT_ONLY")
        self.assertLessEqual(result.started_tick_ns, result.finished_tick_ns)
        for item in fields(result):
            if item.name in ("service_configuration", "service_sid_and_token",
                             "protected_image_publisher_pin_trust", "pipe_sqos_dacl_locality",
                             "image_file_identity", "process_lifetime_until_reply"):
                self.assertEqual(getattr(result, item.name), "UNVERIFIED")
        self.assertEqual(result.pipe_server_process, "UNVERIFIED_CLIENT_HANDLE_API")
        for name in ("allow_spawning", "authorized", "permit", "COMMITTED_FRESH", "spawn"):
            self.assertFalse(hasattr(result, name))
        with self.assertRaises(FrozenInstanceError):
            result.pid = 1
        self.assertEqual(self.fake.closed(),
                         [("CloseHandle", 103), ("CloseServiceHandle", 102),
                          ("CloseServiceHandle", 101)])
        self.assertEqual(self.fake.counts["QueryServiceStatusEx"], 2)
        self.assertEqual(self.fake.counts["GetProcessId"], 2)
        self.assertEqual(self.fake.counts["GetProcessTimes"], 2)
        self.assertEqual(self.fake.counts["WaitForSingleObject"], 3)

    def test_fixed_width_abi_and_pointer_signatures(self):
        self.assertEqual(ctypes.sizeof(probe.DWORD), 4)
        self.assertEqual(ctypes.sizeof(probe.BOOL), 4)
        self.assertEqual(ctypes.sizeof(probe.FILETIME), 8)
        self.assertEqual(ctypes.sizeof(probe.SERVICE_STATUS_PROCESS), 36)
        self.assertEqual([getattr(probe.SERVICE_STATUS_PROCESS, n).offset
                          for n, _ in probe.SERVICE_STATUS_PROCESS._fields_],
                         list(range(0, 36, 4)))
        self.assertEqual(self.fake.api.open_scm.argtypes,
                         [ctypes.c_wchar_p, ctypes.c_wchar_p, probe.DWORD])
        self.assertIs(self.fake.api.open_scm.restype, ctypes.c_void_p)
        self.assertIs(self.fake.api.open_process.restype, ctypes.c_void_p)
        self.assertEqual(self.fake.api.process_times.argtypes,
                         [probe.HANDLE] + [ctypes.POINTER(probe.FILETIME)] * 4)
        self.assertEqual(self.fake.api.query_status.argtypes,
                         [probe.HANDLE, ctypes.c_int32, ctypes.POINTER(ctypes.c_ubyte),
                          probe.DWORD, ctypes.POINTER(probe.DWORD)])
        for fn in (self.fake.api.open_service, self.fake.api.close_service,
                   self.fake.api.wait, self.fake.api.process_id, self.fake.api.close_process):
            self.assertIsNotNone(fn.argtypes)
            self.assertIsNotNone(fn.restype)

    def test_native_loader_system32_only_mocked(self):
        with patch.object(probe.os, "name", "nt"), patch.object(
                probe.ctypes, "WinDLL", side_effect=[self.fake.adv, self.fake.kernel]) as load:
            api = probe._load_native()
        self.assertIs(type(api), probe._NativeCalls)
        self.assertEqual(load.call_args_list[0].args, ("advapi32.dll",))
        self.assertEqual(load.call_args_list[1].args, ("kernel32.dll",))
        self.assertTrue(all(c.kwargs == {"use_last_error": True, "winmode": 0x800}
                            for c in load.call_args_list))
        self.assertEqual(self.fake.events, [])

    def test_unsupported_platform_no_loader_or_opens(self):
        with patch.object(probe.os, "name", "posix"):
            with self.assertRaises(probe.ObservationError) as result:
                probe.observe_service_process()
        self.assertEqual(result.exception.code, "PLATFORM_UNSUPPORTED")
        self.assertEqual(self.fake.events, [])

    def test_bad_timeouts_and_native_injection_rejected_before_calls(self):
        for value in (None, True, False, 0, -1, 5001, 1.5, "1000", 2**64):
            with self.subTest(value=value), self.assertRaises(probe.ObservationError):
                self.observe(timeout_ms=value)
        with self.assertRaises(probe.ObservationError):
            probe.observe_service_process(_calls={"pid": 500})
        self.assertEqual(self.fake.events, [])

    def test_no_external_pid_service_name_handle_or_payload_input(self):
        for key, value in (("pid", 500), ("service_name", "fake"), ("pipe_handle", 123),
                           ("service_verified", True), ("payload", {"pid": 500})):
            with self.subTest(key=key), self.assertRaises(TypeError):
                self.observe(**{key: value})
        self.assertEqual(self.fake.events, [])

    def test_capacity_no_native_calls_and_no_wait_queue(self):
        self.assertTrue(probe._slot.acquire(blocking=False))
        try:
            self.rejected("RESOURCE_EXHAUSTED")
        finally:
            probe._slot.release()
        self.assertEqual(self.fake.events, [])

    def test_reentrant_call_cannot_open_more_handles(self):
        def open_process(*args):
            with self.assertRaises(probe.ObservationError) as result:
                self.observe()
            self.assertEqual(result.exception.stage, "observation_slot")
            return self.fake.OpenProcess(*args)
        self.fake.change[("OpenProcess", 1)] = open_process
        self.observe()
        self.assertEqual(self.fake.counts["OpenSCManagerW"], 1)

    def test_nonzero_bool_is_native_success(self):
        def query(*args):
            self.fake.QueryServiceStatusEx(*args)
            return -1
        self.fake.change[("QueryServiceStatusEx", 1)] = query
        self.observe()

    def test_success_bytes_needed_is_not_a_returned_length(self):
        def query(*args):
            self.fake.QueryServiceStatusEx(*args)
            ctypes.cast(args[-1], ctypes.POINTER(probe.DWORD)).contents.value = 4096
            return 1
        self.fake.change[("QueryServiceStatusEx", 1)] = query
        self.observe()

    def test_errors_preserve_numeric_code_and_do_not_expose_native_exception_text(self):
        self.fake.change[("OpenProcess", 1)] = RuntimeError("SECRET_SENTINEL")
        error = self.rejected("NATIVE_EXCEPTION")
        self.assertNotIn("SECRET_SENTINEL", str(error))
        self.assertEqual(len(self.fake.closed()), 2)

    def test_insufficient_buffer_is_not_retried_or_resized(self):
        self.fake.error = 122
        self.fake.change[("QueryServiceStatusEx", 1)] = 0
        error = self.rejected("WIN32_FAILURE")
        self.assertEqual(error.winerror, 122)
        self.assertEqual(self.fake.counts["QueryServiceStatusEx"], 1)

    def test_resource_error_taxonomy_no_retry_or_rights_fallback(self):
        for value in (4, 8, 14, 1450, 1816):
            with self.subTest(error=value):
                self.fake = FakeWin32()
                self.fake.error = value
                self.fake.change[("OpenProcess", 1)] = None
                result = self.rejected("RESOURCE_EXHAUSTED")
                self.assertEqual(result.winerror, value)
                self.assertEqual(self.fake.counts["OpenProcess"], 1)
                self.assertEqual(self.fake.closed(), ALL_CLOSES[1:])

    def test_invalid_last_error_type_fails_without_retry(self):
        self.fake.error = True
        self.fake.change[("OpenProcess", 1)] = None
        self.rejected("INVALID_NATIVE_RESULT")
        self.assertEqual(self.fake.closed(), ALL_CLOSES[1:])

    def test_process_id_mismatch_at_both_samples(self):
        for occurrence in (1, 2):
            with self.subTest(sample=occurrence):
                self.fake = FakeWin32()
                self.fake.change[("GetProcessId", occurrence)] = 999
                self.rejected("PROCESS_CHANGED")
                self.assertEqual(self.fake.closed(), ALL_CLOSES)

    def test_malformed_close_result_fails_and_blocks_reopen(self):
        self.fake.change[("CloseHandle", 1)] = True
        self.rejected("CLEANUP_UNCONFIRMED")
        self.assertTrue(probe._cleanup_uncertain)
        self.assertEqual(self.fake.closed(), ALL_CLOSES)

    def test_late_recheck_rejects_and_closes_all(self):
        tick = [0]
        def query(*args):
            value = self.fake.QueryServiceStatusEx(*args)
            tick[0] = 1_000_000
            return value
        self.fake.change[("QueryServiceStatusEx", 2)] = query
        with patch.object(probe.time, "monotonic_ns", side_effect=lambda: tick[0]):
            with self.assertRaises(probe.ObservationError) as result:
                self.observe(timeout_ms=1)
        self.assertEqual(result.exception.code, "DEADLINE_OR_CLOCK_INVALID")
        self.assertEqual(self.fake.closed(), ALL_CLOSES)

    def test_deadline_after_open_keeps_ownership_for_cleanup(self):
        with patch.object(probe.time, "monotonic_ns", side_effect=[0, 0, 1_000_000]):
            with self.assertRaises(probe.ObservationError) as result:
                self.observe(timeout_ms=1)
        self.assertEqual(result.exception.code, "DEADLINE_OR_CLOCK_INVALID")
        self.assertEqual(self.fake.closed(), [("CloseServiceHandle", 101)])

    def test_clock_rollback_rejected(self):
        with patch.object(probe.time, "monotonic_ns", side_effect=[10, 9]):
            self.rejected("DEADLINE_OR_CLOCK_INVALID")
        self.assertEqual(self.fake.events, [])

    def test_close_time_counts_toward_deadline(self):
        tick = [0]
        def close(handle):
            tick[0] = 2_000_000
            return 1
        self.fake.change[("CloseHandle", 1)] = close
        with patch.object(probe.time, "monotonic_ns", side_effect=lambda: tick[0]):
            with self.assertRaises(probe.ObservationError) as result:
                self.observe(timeout_ms=1)
        self.assertEqual(result.exception.stage, "cleanup.deadline")
        self.assertEqual(len(self.fake.closed()), 3)

    def test_close_failures_all_attempted_once_latch_future_admission(self):
        self.fake.change[("CloseHandle", 1)] = 0
        self.fake.change[("CloseServiceHandle", 1)] = RuntimeError("SECRET")
        self.fake.change[("CloseServiceHandle", 2)] = 0
        error = self.rejected("CLEANUP_UNCONFIRMED")
        self.assertEqual(len(error.cleanup_failures), 3)
        self.assertEqual(len(self.fake.closed()), 3)
        before = len(self.fake.events)
        self.rejected("CLEANUP_UNCONFIRMED")
        self.assertEqual(len(self.fake.events), before)

    def test_primary_failure_kept_with_cleanup_failure(self):
        self.fake.change[("GetProcessId", 1)] = 999
        self.fake.change[("CloseHandle", 1)] = 0
        error = self.rejected("PROCESS_CHANGED")
        self.assertEqual(len(error.cleanup_failures), 1)
        self.assertEqual(len(self.fake.closed()), 3)

    def test_keyboard_interrupt_closes_handles_and_releases_slot(self):
        self.fake.change[("GetProcessId", 1)] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.observe()
        self.assertEqual(len(self.fake.closed()), 3)
        self.assertTrue(probe._slot.acquire(blocking=False))
        probe._slot.release()

    def test_cleanup_exception_does_not_skip_later_closes(self):
        self.fake.change[("CloseHandle", 1)] = KeyboardInterrupt()
        error = self.rejected("CLEANUP_UNCONFIRMED")
        self.assertEqual(len(error.cleanup_failures), 1)
        self.assertEqual(len(self.fake.closed()), 3)

    def test_production_inventory_complete_and_no_observer_import(self):
        filename = "offline_b04_service_identity_probe.py"
        self.assertIn(filename, DEV_ONLY_MODULES)
        self.assertNotIn(filename, MODULES)
        root = Path(__file__).resolve().parents[1] / "src" / "sora_commander_next"
        self.assertEqual({p.name for p in root.glob("*.py")} - OPTIONAL_DEV_ONLY_MODULES,
                         set(MODULES) | (DEV_ONLY_MODULES - OPTIONAL_DEV_ONLY_MODULES))
        for name in MODULES:
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or "", *(a.name for a in node.names)]
                else:
                    continue
                self.assertFalse(any(filename[:-3] in n.split(".") for n in names), name)


# Separate methods make the actual unittest count reflect every native fault
# location, including rechecks. Each one exercises real adapter/orchestration.
def fault_test(name, occurrence, value, expected_closes):
    def run(self):
        self.fake.change[(name, occurrence)] = value
        self.rejected()
        self.assertEqual(self.fake.closed(), expected_closes)
        self.assertEqual(self.fake.counts[name], occurrence)
    return run


ALL_CLOSES = [("CloseHandle", 103), ("CloseServiceHandle", 102),
              ("CloseServiceHandle", 101)]
for name, occurrence, closes in (
    ("OpenSCManagerW", 1, []),
    ("OpenServiceW", 1, ALL_CLOSES[2:]),
    ("QueryServiceStatusEx", 1, ALL_CLOSES[1:]),
    ("OpenProcess", 1, ALL_CLOSES[1:]),
    ("WaitForSingleObject", 1, ALL_CLOSES),
    ("GetProcessId", 1, ALL_CLOSES),
    ("GetProcessTimes", 1, ALL_CLOSES),
    ("QueryServiceStatusEx", 2, ALL_CLOSES),
    ("WaitForSingleObject", 2, ALL_CLOSES),
    ("GetProcessId", 2, ALL_CLOSES),
    ("GetProcessTimes", 2, ALL_CLOSES),
    ("WaitForSingleObject", 3, ALL_CLOSES),
):
    for label, value in (("denied", 0), ("exception", RuntimeError("private")),
                         ("memory", MemoryError())):
        setattr(NativeObservationTests, f"test_fault_{name}_{occurrence}_{label}",
                fault_test(name, occurrence, value, closes))


def state_test(field_name, value, occurrence):
    def run(self):
        def query(*args):
            self.fake.QueryServiceStatusEx(*args)
            data = ctypes.cast(args[2], ctypes.POINTER(probe.SERVICE_STATUS_PROCESS)).contents
            setattr(data, field_name, value)
            return 1
        self.fake.change[("QueryServiceStatusEx", occurrence)] = query
        self.rejected()
        self.assertEqual(self.fake.closed(), ALL_CLOSES[1:] if occurrence == 1 else ALL_CLOSES)
    return run


for field_name, value in (("dwServiceType", 0x20), ("dwServiceType", 0x110),
                          ("dwCurrentState", 1), ("dwCurrentState", 2),
                          ("dwCurrentState", 3), ("dwCurrentState", 7),
                          ("dwProcessId", 0), ("dwProcessId", 501),
                          ("dwServiceFlags", 1), ("dwCheckPoint", 1),
                          ("dwWaitHint", 1), ("dwWin32ExitCode", 5),
                          ("dwServiceSpecificExitCode", 5), ("dwControlsAccepted", 1)):
    # Different nonzero PID/controls are meaningful only at the second sample.
    for occurrence in ((2,) if field_name in ("dwControlsAccepted",) or
                       (field_name == "dwProcessId" and value == 501) else (1, 2)):
        setattr(NativeObservationTests, f"test_state_{field_name}_{value}_{occurrence}",
                state_test(field_name, value, occurrence))


def invalid_return_test(name, value):
    def run(self):
        self.fake.change[(name, 1)] = value
        self.rejected()
    return run


for name in ("OpenSCManagerW", "OpenServiceW", "OpenProcess"):
    for label, value in (("null", None), ("bool", True), ("float", 1.5),
                         ("negative", -1), ("invalid", probe.INVALID_HANDLE),
                         ("pseudo", probe.INVALID_HANDLE - 1),
                         ("overflow", probe.INVALID_HANDLE + 1), ("string", "103")):
        setattr(NativeObservationTests, f"test_invalid_{name}_{label}",
                invalid_return_test(name, value))
for name in ("GetProcessId", "WaitForSingleObject", "QueryServiceStatusEx", "GetProcessTimes"):
    for label, value in (("bool", True), ("missing", None), ("float", 1.5),
                         ("overflow", 2**32)):
        setattr(NativeObservationTests, f"test_invalid_{name}_{label}",
                invalid_return_test(name, value))


def time_test(kind, occurrence):
    def run(self):
        def changed(handle, created, exited, kernel, user):
            self.fake.GetProcessTimes(handle, created, exited, kernel, user)
            data = ctypes.cast(created if kind != "exited" else exited,
                               ctypes.POINTER(probe.FILETIME)).contents
            if kind == "zero":
                data.dwHighDateTime = data.dwLowDateTime = 0
            else:
                data.dwLowDateTime += 1
            return 1
        self.fake.change[("GetProcessTimes", occurrence)] = changed
        self.rejected()
        self.assertEqual(self.fake.closed(), ALL_CLOSES)
    return run


for kind, occurrence in (("zero", 1), ("zero", 2), ("changed", 2)):
    setattr(NativeObservationTests, f"test_process_time_{kind}_{occurrence}",
            time_test(kind, occurrence))


def running_exit_time_test(occurrence):
    """GetProcessTimes lpExitTime is undefined until process termination."""
    def run(self):
        def native(handle, created, exited, kernel, user):
            self.fake.GetProcessTimes(handle, created, exited, kernel, user)
            value = ctypes.cast(exited, ctypes.POINTER(probe.FILETIME)).contents
            value.dwLowDateTime = 0xDEADBEEF
            value.dwHighDateTime = 0x12345678
            return 1
        self.fake.change[("GetProcessTimes", occurrence)] = native
        result = self.observe()
        self.assertEqual(result.authentication, "NOT_AUTHENTICATED")
        self.assertEqual(result.pid, 500)
        self.assertEqual(self.fake.closed(), ALL_CLOSES)
        self.assertEqual(self.fake.counts["WaitForSingleObject"], 3)
    return run


for occurrence in (1, 2):
    setattr(NativeObservationTests,
            f"test_running_undefined_exit_time_{occurrence}",
            running_exit_time_test(occurrence))
for occurrence in (1, 2, 3):
    for label, value in (("failed", 0xFFFFFFFF), ("abandoned", 128), ("unknown", 7)):
        setattr(NativeObservationTests, f"test_wait_{label}_{occurrence}",
                fault_test("WaitForSingleObject", occurrence, value, ALL_CLOSES))


if __name__ == "__main__":
    unittest.main()
