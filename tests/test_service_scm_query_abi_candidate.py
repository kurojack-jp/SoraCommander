"""Windows SCM variable-buffer ABI tests using fake DLL functions only.

No ctypes.WinDLL, OpenSCManager, native service HANDLE, WinVerifyTrust, IPC,
SQLite or process is ever called. Fake functions inspect/write ctypes buffers.
"""
import ctypes
from dataclasses import replace
import unittest
from unittest.mock import patch

from sora_commander_next.offline_b04_service_identity_probe import SERVICE_NAME
from sora_commander_next.service_scm_sampling_candidate import (
    ScmSamplingError, sample_scm_configuration)
from sora_commander_next.service_scm_query_abi_candidate import (
    ExplicitScmQueryBackend, QUERY_SERVICE_CONFIGW, SERVICE_SID_INFO,
    DWORD, MAX_QUERY_BYTES, ERROR_INSUFFICIENT_BUFFER, SERVICE_CONFIG_SERVICE_SID_INFO,
)


def assign(ptr, value):
    ctypes.cast(ptr, ctypes.POINTER(DWORD))[0] = value


class Function:
    def __init__(self, fn):
        self.fn = fn
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.fn(*args)


class FakeDll:
    def __init__(self):
        self.events = []
        self.error = 0
        self.probe_needed = 220
        self.sid_needed = 4
        self.probe_error = ERROR_INSUFFICIENT_BUFFER
        self.fail_fill = False
        self.grow_fill = False
        self.bad_string = None
        self.bad_account = None
        self.changed = False
        self.close_return = True
        self.fail_probe = False
        self.config_count = 0
        self.handles = []
        self.OpenSCManagerW = Function(self.open_scm)
        self.OpenServiceW = Function(self.open_service)
        self.QueryServiceConfigW = Function(self.query)
        self.QueryServiceConfig2W = Function(self.query2)
        self.CloseServiceHandle = Function(self.close)

    def open_scm(self, machine, database, access):
        self.events.append(("open_scm", machine, database, access.value))
        return 10

    def open_service(self, scm, name, access):
        self.events.append(("open_service", scm.value, name, access.value))
        return 20

    def _probe(self, needed, ptr, length, out):
        assign(out, needed)
        self.error = self.probe_error
        if self.fail_probe:
            return 0
        return 0

    def query(self, handle, ptr, size, out):
        if not ptr:
            self.events.append(("probe_config", handle.value, size))
            return self._probe(self.probe_needed, ptr, size, out)
        self.config_count += 1
        self.events.append(("fill_config", handle.value, size))
        if self.fail_fill:
            self.error=5
            return 0
        if self.grow_fill:
            assign(out, size + 1)
            return 1
        assign(out, 0)
        base = ptr.value
        view = QUERY_SERVICE_CONFIGW.from_address(base)
        view.dwServiceType = 16
        view.dwStartType = 3
        view.dwErrorControl = 1
        path = ('"C:\\Protected\\ledger.exe"' if not self.changed or self.config_count == 1
                else '"C:\\Elsewhere\\ledger.exe"')
        encoded_path = path.encode("utf-16-le")+b"\0\0"
        encoded_account = "NT AUTHORITY\\LocalService".encode("utf-16-le")+b"\0\0"
        loc = ctypes.sizeof(QUERY_SERVICE_CONFIGW)
        if self.bad_string == "missing":
            view.lpBinaryPathName = 0
        elif self.bad_string == "outside":
            view.lpBinaryPathName = base + size + 2
        elif self.bad_string == "inside_struct":
            view.lpBinaryPathName = base + 6
        elif self.bad_string == "unaligned":
            view.lpBinaryPathName = base + loc + 1
        else:
            view.lpBinaryPathName = base + loc
            if self.bad_string == "unterminated":
                # All bytes remaining nonzero, so the scan never sees a NUL.
                ctypes.memset(base + loc, 0x41, size - loc)
            elif self.bad_string == "surrogate":
                ctypes.memmove(base + loc, b"\x00\xd8\0\0", 4)
            else:
                ctypes.memmove(base + loc, encoded_path, len(encoded_path))
        account_loc = loc + 66
        if self.bad_account == "outside":
            view.lpServiceStartName = base - 2
        else:
            view.lpServiceStartName = base + account_loc
            if self.bad_string != "unterminated":
                ctypes.memmove(base + account_loc, encoded_account, len(encoded_account))
        return 1

    def query2(self, handle, level, ptr, size, out):
        if not ptr:
            self.events.append(("probe_sid", handle.value, level, size))
            return self._probe(self.sid_needed, ptr, size, out)
        self.events.append(("fill_sid", handle.value, level, size))
        if self.fail_fill:
            self.error=5
            return 0
        assign(out, 0)
        view = SERVICE_SID_INFO.from_address(ptr.value)
        view.dwServiceSidType=3
        return 1

    def close(self, handle):
        self.events.append(("close", handle.value))
        return 1 if self.close_return else 0

    def last_error(self):
        return self.error


class NativeScmAbiTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDll()
        self.backend = ExplicitScmQueryBackend(self.fake, self.fake.last_error)

    def test_binds_documented_abi_without_native_loader(self):
        with patch("ctypes.WinDLL", create=True) as loader:
            ExplicitScmQueryBackend(FakeDll(), lambda: 122)
            loader.assert_not_called()
        self.assertEqual(len(self.fake.QueryServiceConfigW.argtypes), 4)
        self.assertEqual(len(self.fake.QueryServiceConfig2W.argtypes), 5)
        self.assertEqual(len(self.fake.OpenServiceW.argtypes), 3)
        self.assertEqual(self.fake.QueryServiceConfig2W.restype, ctypes.c_int32)

    def test_query_config_with_owned_buffer_and_bounded_strings(self):
        result = self.backend.query_config(20)
        self.assertEqual(result, ('"C:\\Protected\\ledger.exe"',
                                  "NT AUTHORITY\\LocalService", 16, 3, 1))
        self.assertEqual(self.fake.events[0], ("probe_config", 20, 0))
        self.assertEqual(self.fake.events[1], ("fill_config", 20, 220))

    def test_sid_type_is_level_five_and_dword(self):
        self.assertEqual(self.backend.query_sid_type(20), 3)
        self.assertEqual(self.fake.events[0], ("probe_sid", 20, 5, 0))
        self.assertEqual(self.fake.events[1], ("fill_sid", 20, 5, 4))

    def test_same_native_fake_service_handle_sample_still_untrusted(self):
        tick = iter(range(100,120))
        result = sample_scm_configuration(_backend=self.backend,
                                           _clock_ms=lambda: next(tick))
        self.assertEqual(result.authentication, "NOT_AUTHENTICATED")
        self.assertEqual(result.admission, "UNAVAILABLE")
        self.assertTrue(result.unchanged)
        self.assertEqual(result.first.binary_command_line,'"C:\\Protected\\ledger.exe"')
        self.assertEqual(self.fake.events[-2:], [("close",20),("close",10)])

    def test_service_command_mismatch_between_samples_denied(self):
        self.fake.changed=True
        with self.assertRaises(ScmSamplingError) as failure:
            sample_scm_configuration(_backend=self.backend, _clock_ms=lambda: 100)
        self.assertEqual(failure.exception.code, "SCM_CONFIG_CHANGED")
        self.assertEqual(self.fake.events[-2:], [("close",20),("close",10)])

    def test_config_buffer_upper_bound(self):
        for size in (0, 1, MAX_QUERY_BYTES+1):
            with self.subTest(size=size):
                self.fake.probe_needed=size
                with self.assertRaises(ScmSamplingError):
                    self.backend.query_config(20)

    def test_sid_buffer_upper_bound(self):
        for size in (0, 1, MAX_QUERY_BYTES+1):
            with self.subTest(size=size):
                self.fake.sid_needed=size
                with self.assertRaises(ScmSamplingError):
                    self.backend.query_sid_type(20)

    def test_probe_must_fail_for_insufficient_buffer(self):
        for error in (0, 5, 6, 87, True):
            with self.subTest(error=error):
                self.fake.probe_error=error
                with self.assertRaisesRegex(ScmSamplingError, "UNEXPECTED_SCM_PROBE"):
                    self.backend.query_config(20)

    def test_no_unbounded_resize_after_second_query_error(self):
        self.fake.fail_fill=True
        with self.assertRaisesRegex(ScmSamplingError, "SCM_CONFIG_READ_FAILED"):
            self.backend.query_config(20)
        self.assertEqual(len([x for x in self.fake.events if x[0]=="fill_config"]),1)

    def test_error_if_second_size_exceeds_buffer(self):
        self.fake.grow_fill=True
        with self.assertRaisesRegex(ScmSamplingError, "INCONSISTENT_SCM_BUFFER_SIZE"):
            self.backend.query_config(20)

    def test_reject_bad_binary_path_pointers(self):
        for kind in ("missing","outside","inside_struct","unaligned"):
            with self.subTest(kind=kind):
                self.fake.bad_string=kind
                with self.assertRaisesRegex(ScmSamplingError, "SCM_STRING_POINTER_OUTSIDE_BUFFER"):
                    self.backend.query_config(20)

    def test_reject_outside_account_pointer(self):
        self.fake.bad_account="outside"
        with self.assertRaisesRegex(ScmSamplingError, "SCM_STRING_POINTER_OUTSIDE_BUFFER"):
            self.backend.query_config(20)

    def test_reject_missing_utf16_nul(self):
        self.fake.bad_string="unterminated"
        with self.assertRaisesRegex(ScmSamplingError, "SCM_STRING_UNTERMINATED"):
            self.backend.query_config(20)

    def test_reject_utf16_unpaired_surrogate(self):
        self.fake.bad_string="surrogate"
        with self.assertRaises(ScmSamplingError):
            self.backend.query_config(20)

    def test_open_functions_use_least_access_only(self):
        self.assertEqual(self.backend.open_scm(1),10)
        self.assertEqual(self.backend.open_service(10,SERVICE_NAME,1),20)
        self.assertEqual(self.fake.events[:2], [
            ("open_scm",None,None,1),("open_service",10,SERVICE_NAME,1)])

    def test_strict_rights_and_service_name(self):
        for n in (0,3,True,-1,"1"):
            with self.subTest(n=n), self.assertRaises(ScmSamplingError):
                self.backend.open_scm(n)
        for n in (0,3,True,-1,"1"):
            with self.subTest(n=n), self.assertRaises(ScmSamplingError):
                self.backend.open_service(10,SERVICE_NAME,n)
        with self.assertRaises(ScmSamplingError):
            self.backend.open_service(10,"SomeoneElse",1)
        self.assertEqual(self.fake.events, [])

    def test_reject_invalid_handles_without_calling_native(self):
        for val in (0,-1,True,None,"2"):
            with self.subTest(val=val):
                with self.assertRaises(ScmSamplingError):
                    self.backend.query_config(val)
                with self.assertRaises(ScmSamplingError):
                    self.backend.query_sid_type(val)
        self.assertEqual(self.fake.events, [])

    def test_reject_unconfirmed_close(self):
        self.fake.close_return=False
        self.assertFalse(self.backend.close(20))
        self.assertEqual(self.fake.events, [("close",20)])

    def test_no_trust_keyword_constructor(self):
        with self.assertRaises(TypeError):
            ExplicitScmQueryBackend(self.fake,self.fake.last_error,authenticated=True)

    def test_last_error_accessor_is_required(self):
        with self.assertRaises(ScmSamplingError):
            ExplicitScmQueryBackend(self.fake,None)

    def test_win64_struct_layout_checked_without_os_call(self):
        self.assertEqual(ctypes.sizeof(QUERY_SERVICE_CONFIGW), 64)
        self.assertEqual(QUERY_SERVICE_CONFIGW.lpBinaryPathName.offset, 16)
        self.assertEqual(QUERY_SERVICE_CONFIGW.lpServiceStartName.offset, 48)
        self.assertEqual(ctypes.sizeof(SERVICE_SID_INFO), 4)

    def test_native_mock_config_matches_old_b04_policy_but_no_authority(self):
        from sora_commander_next.offline_b04_service_identity_probe import ServiceProcessObservation
        from sora_commander_next.service_native_prerequisites import NativePrerequisites
        from sora_commander_next.service_auth_candidate import (
            ReleasePolicyCandidate, compare_authentication_prerequisites)
        policy = ReleasePolicyCandidate(
            "a"*32, "b"*32, 1, r"C:\Protected\ledger.exe",
            "c"*64, "S-1-5-80-1-2-3-4-5")
        native = NativePrerequisites(
            ServiceProcessObservation(SERVICE_NAME, 42, 100, 1, 2),
            policy.image_path, (1, "d"*32, 100), "S-1-5-19",
            ((policy.service_sid, 4),), ((policy.service_sid, 0),), 1)
        samples = sample_scm_configuration(
            _backend=self.backend, _clock_ms=lambda: 100)
        result = compare_authentication_prerequisites(
            policy, samples.first, samples.last, native)
        self.assertTrue(result.configuration_matches)
        self.assertEqual(result.authentication, "NOT_AUTHENTICATED")
        self.assertEqual(result.admission, "UNAVAILABLE")
        self.assertFalse(samples.publisher_pinned)
        self.assertFalse(samples.connected_client_pipe_bound)

    def test_bogus_native_open_handle_is_rejected(self):
        for fake_handle in (0, -1, True, 1<<64, None):
            with self.subTest(value=fake_handle):
                dll = FakeDll()
                dll.OpenSCManagerW = Function(lambda *_: fake_handle)
                backend = ExplicitScmQueryBackend(dll, dll.last_error)
                with self.assertRaises(ScmSamplingError):
                    backend.open_scm(1)

    def test_bad_sid_probe_never_forwards_to_config(self):
        self.fake.sid_needed = 0
        with self.assertRaisesRegex(ScmSamplingError, "INVALID_SCM_BUFFER_BOUND"):
            self.backend.query_sid_type(20)
        self.assertFalse(any(x[0]=="fill_sid" for x in self.fake.events))

    def test_declared_no_live_auth_even_after_fake_native_roundtrip(self):
        result = sample_scm_configuration(
            _backend=self.backend, _clock_ms=lambda: 100)
        for name in ("native_service_handle_verified", "connected_client_pipe_bound",
                     "publisher_pinned", "installed_release_verified",
                     "service_process_retained"):
            self.assertIs(getattr(result, name), False)
        self.assertEqual(result.authentication, "NOT_AUTHENTICATED")

    def test_no_actual_native_dll_or_loader_wired(self):
        import pathlib
        source=(pathlib.Path(__file__).resolve().parents[1]/
                "src/sora_commander_next/service_scm_query_abi_candidate.py").read_text("utf-8")
        for forbidden in ("ctypes.WinDLL(", "ctypes.windll", "sc.exe", "subprocess.",
                          "CreateProcess", "WinVerifyTrust(", "sqlite3.connect"):
            self.assertNotIn(forbidden,source)


if __name__ == "__main__":
    unittest.main()
