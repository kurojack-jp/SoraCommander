"""Complete Win32 injection. No native lookup, pipe, token or image probing."""
import ctypes as C
from dataclasses import FrozenInstanceError
from unittest.mock import Mock, patch
import unittest

from test_offline_b04_service_identity_probe import FakeWin32
from sora_commander_next import offline_b04_service_identity_probe as base
from sora_commander_next import service_native_prerequisites as subject

PATH = r"C:\Protected\ledger.exe"
SERVICE_SID = "S-1-5-80-1-2-3-4-5"


def sid_bytes(text):
    parts = [int(p) for p in text[2:].split("-")]
    return bytes([parts[0], len(parts) - 2]) + parts[1].to_bytes(6, "big") + b"".join(
        p.to_bytes(4, "little") for p in parts[2:])


class FakeNative(FakeWin32):
    def __init__(self):
        super().__init__()
        self.overrides = {}
        self.extra_events = []
        self.extra_counts = {}
        for dll, names in (
                (self.adv, ("OpenProcessToken", "GetTokenInformation")),
                (self.kernel, ("QueryFullProcessImageNameW", "CreateFileW",
                               "GetFileInformationByHandleEx", "GetFinalPathNameByHandleW"))):
            for name in names:
                def invoke(*args, name=name):
                    self.extra_events.append((name, args))
                    n = self.extra_counts.get(name, 0) + 1
                    self.extra_counts[name] = n
                    override = self.overrides.get((name, n))
                    if isinstance(override, BaseException):
                        raise override
                    if callable(override):
                        return override(*args)
                    if (name, n) in self.overrides:
                        return override
                    return getattr(self, name)(*args)
                setattr(dll, name, Mock(side_effect=invoke))
        self.api = subject._NativeCalls(self.adv, self.kernel, lambda: self.error)

    def QueryFullProcessImageNameW(self, handle, flags, buf, count):
        assert (handle, flags) == (103, 0)
        buf.value = PATH
        C.cast(count, C.POINTER(base.DWORD)).contents.value = len(PATH)
        return 1

    def CreateFileW(self, path, access, share, security, creation, flags, template):
        assert (path, access, share, security, creation, flags, template) == (
            PATH, 0x80, 1, None, 3, 0x200000, None)
        return 104

    def GetFileInformationByHandleEx(self, handle, kind, pointer, size):
        assert handle == 104
        classes = {18: subject.FILE_ID_INFO, 9: subject.FILE_ATTRIBUTE_TAG_INFO,
                   1: subject.FILE_STANDARD_INFO}
        assert size == C.sizeof(classes[kind])
        record = C.cast(pointer, C.POINTER(classes[kind])).contents
        if kind == 18:
            record.volume = 7
            record.identifier[0] = 12
        elif kind == 9:
            record.attributes = 0x20
        else:
            record.links, record.size, record.allocation = 1, 4096, 4096
        return 1

    def GetFinalPathNameByHandleW(self, handle, buf, capacity, flags):
        assert (handle, capacity, flags) == (104, 32768, 0)
        buf.value = "\\\\?\\" + PATH
        return len(buf.value)

    def OpenProcessToken(self, handle, access, token):
        assert (handle, access) == (103, 8)
        C.cast(token, C.POINTER(base.HANDLE)).contents.value = 105
        return 1

    def GetTokenInformation(self, handle, kind, buf, size, needed):
        assert (handle, size) == (105, 65536)
        if kind == 8:
            C.cast(buf, C.POINTER(base.DWORD)).contents.value = 1
            used = 4
        elif kind == 1:
            entry = subject.SID_AND_ATTRIBUTES.from_buffer(buf)
            offset = C.sizeof(subject.SID_AND_ATTRIBUTES)
            data = sid_bytes("S-1-5-19")
            entry.sid = C.addressof(buf) + offset
            C.memmove(entry.sid, data, len(data))
            used = offset + len(data)
        else:
            assert kind in (2, 11)
            head = subject.TOKEN_GROUPS_HEAD.from_buffer(buf)
            head.count = 1
            offset = C.sizeof(subject.TOKEN_GROUPS_HEAD)
            data = sid_bytes(SERVICE_SID)
            head.first.sid, head.first.attributes = C.addressof(buf) + offset, 4
            C.memmove(head.first.sid, data, len(data))
            used = offset + len(data)
        C.cast(needed, C.POINTER(base.DWORD)).contents.value = used
        return 1

    def CloseHandle(self, handle):
        assert handle in (103, 104, 105)
        return 1


class PrerequisiteTests(unittest.TestCase):
    def setUp(self):
        for target in ("ctypes.WinDLL", "subprocess.Popen", "os.system"):
            guard = patch(target, side_effect=AssertionError("native operation forbidden"), create=True)
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.object(base, "_cleanup_uncertain", False)
        guard.start()
        self.addCleanup(guard.stop)
        self.fake = FakeNative()

    def observe(self, **kw):
        return subject.observe_connected_service_prerequisites(200,
            connection_epoch=1, _calls=self.fake.api, **kw)

    def deny(self):
        with self.assertRaises(base.ObservationError) as result:
            self.observe()
        return result.exception

    def test_complete_native_prerequisites_remain_unavailable(self):
        result = self.observe()
        self.assertEqual(result.authentication, "NOT_AUTHENTICATED")
        self.assertEqual(result.admission, "UNAVAILABLE")
        self.assertEqual(result.image_path, PATH)
        self.assertEqual(result.image_identity, (7, "0c" + "00" * 15, 4096))
        self.assertEqual(result.token_user, "S-1-5-19")
        self.assertEqual(result.service_groups, ((SERVICE_SID, 4),))
        self.assertEqual(len(result.missing_evidence), 8)
        self.assertIn("CLIENT_HANDLE_PID_CONTRACT_NOT_ACCEPTED", result.missing_evidence)
        with self.assertRaises(FrozenInstanceError):
            result.admission = "AUTHENTICATED"

    def test_borrowed_pipe_is_never_closed_or_queried(self):
        self.observe()
        self.assertEqual(self.fake.closed(), [("CloseHandle", 105), ("CloseHandle", 104),
            ("CloseHandle", 103), ("CloseServiceHandle", 102), ("CloseServiceHandle", 101)])
        self.assertNotIn("GetNamedPipeServerProcessId", self.fake.counts)

    def test_abi_layout_and_read_only_rights(self):
        self.assertEqual(C.sizeof(subject.FILE_ID_INFO), 24)
        self.assertEqual(C.sizeof(subject.FILE_ATTRIBUTE_TAG_INFO), 8)
        self.assertEqual(C.sizeof(subject.FILE_STANDARD_INFO), 24)
        self.assertEqual(subject.TOKEN_GROUPS_HEAD.first.offset, 8)
        self.assertEqual(self.fake.api.open_token.argtypes,
                         [base.HANDLE, base.DWORD, C.POINTER(base.HANDLE)])
        self.assertEqual(self.fake.api.image_name.argtypes,
                         [base.HANDLE, base.DWORD, C.c_wchar_p, C.POINTER(base.DWORD)])
        self.assertEqual(self.fake.api.file_info.argtypes,
                         [base.HANDLE, C.c_int32, C.c_void_p, base.DWORD])

    def test_invalid_input_no_native_calls(self):
        for value in (True, None, 0, -1, base.INVALID_HANDLE, "200"):
            with self.subTest(value=value), self.assertRaises(base.ObservationError):
                subject.observe_connected_service_prerequisites(value, connection_epoch=1,
                                                                _calls=self.fake.api)
        for key, value in (("trusted", True), ("publisher_hash", "a" * 64),
                           ("expected_service_sid", SERVICE_SID)):
            with self.assertRaises(TypeError):
                self.observe(**{key: value})
        self.assertEqual(self.fake.events, [])

    def test_final_scm_rebind_closes_nested_and_process_handles(self):
        def changed(*args):
            self.fake.QueryServiceStatusEx(*args)
            C.cast(args[2], C.POINTER(base.SERVICE_STATUS_PROCESS)).contents.dwProcessId = 999
            return 1
        self.fake.change[("QueryServiceStatusEx", 2)] = changed
        self.assertEqual(self.deny().code, "SERVICE_CHANGED")
        self.assertEqual(len(self.fake.closed()), 5)

    def test_process_exit_after_nested_observation_denied(self):
        self.fake.change[("WaitForSingleObject", 2)] = 0
        self.deny()
        self.assertEqual(len(self.fake.closed()), 5)

    def test_changed_image_path_denied(self):
        def changed(*args):
            self.fake.QueryFullProcessImageNameW(*args)
            args[2].value = PATH.replace("ledger", "forged")
            C.cast(args[3], C.POINTER(base.DWORD)).contents.value = len(args[2].value)
            return 1
        self.fake.overrides[("QueryFullProcessImageNameW", 2)] = changed
        self.deny()

    def test_final_path_does_not_match_opened_name(self):
        def changed(handle, buf, size, flags):
            buf.value = r"\\?\C:\Attacker\ledger.exe"
            return len(buf.value)
        self.fake.overrides[("GetFinalPathNameByHandleW", 1)] = changed
        self.deny()

    def test_invalid_sid_pointer_rejected_before_dereference(self):
        def invalid(*args):
            self.fake.GetTokenInformation(*args)
            subject.SID_AND_ATTRIBUTES.from_buffer(args[2]).sid = 1
            return 1
        self.fake.overrides[("GetTokenInformation", 2)] = invalid
        self.deny()

    def test_restricted_sid_zero_attributes_are_observed_not_interpreted(self):
        def restricted(*args):
            self.fake.GetTokenInformation(*args)
            subject.TOKEN_GROUPS_HEAD.from_buffer(args[2]).first.attributes = 0
            return 1
        for occurrence in (4, 8):
            self.fake.overrides[("GetTokenInformation", occurrence)] = restricted
        result = self.observe()
        self.assertEqual(result.restricted_service_groups, ((SERVICE_SID, 0),))
        self.assertEqual(result.admission, "UNAVAILABLE")

    def test_normal_service_sid_must_be_enabled(self):
        def disabled(*args):
            self.fake.GetTokenInformation(*args)
            subject.TOKEN_GROUPS_HEAD.from_buffer(args[2]).first.attributes = 0
            return 1
        self.fake.overrides[("GetTokenInformation", 3)] = disabled
        self.deny()

    def test_group_count_cannot_read_outside_returned_buffer(self):
        def invalid(*args):
            self.fake.GetTokenInformation(*args)
            subject.TOKEN_GROUPS_HEAD.from_buffer(args[2]).count = 257
            return 1
        self.fake.overrides[("GetTokenInformation", 3)] = invalid
        self.deny()

    def test_token_changed_between_samples(self):
        def changed(*args):
            self.fake.GetTokenInformation(*args)
            head = subject.TOKEN_GROUPS_HEAD.from_buffer(args[2])
            C.memmove(head.first.sid, sid_bytes("S-1-5-80-6-7-8-9-1"), 32)
            return 1
        self.fake.overrides[("GetTokenInformation", 7)] = changed
        self.deny()

    def test_close_uncertainty_latches_future_observation(self):
        self.fake.change[("CloseHandle", 1)] = 0
        self.assertEqual(self.deny().code, "CLEANUP_UNCONFIRMED")
        count = len(self.fake.events)
        self.deny()
        self.assertEqual(len(self.fake.events), count)
        self.assertEqual(len(self.fake.closed()), 5)

    def test_deadline_during_token_query_closes_all_handles(self):
        tick = [0]
        def delayed(*args):
            self.fake.GetTokenInformation(*args)
            tick[0] = 1_000_000
            return 1
        self.fake.overrides[("GetTokenInformation", 1)] = delayed
        with patch.object(base.time, "monotonic_ns", side_effect=lambda: tick[0]):
            self.assertEqual(self._timed_deny(), "DEADLINE_OR_CLOCK_INVALID")
        self.assertEqual(len(self.fake.closed()), 5)

    def _timed_deny(self):
        with self.assertRaises(base.ObservationError) as caught:
            self.observe(timeout_ms=1)
        return caught.exception.code

    def test_interrupt_during_inspection_closes_before_propagating(self):
        self.fake.overrides[("GetTokenInformation", 1)] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.observe()
        self.assertEqual(len(self.fake.closed()), 5)


def fault_case(name, occurrence, value):
    def run(self):
        self.fake.overrides[(name, occurrence)] = value
        self.deny()
        # Exactly one call at fault, no fallback rights/query retry.
        self.assertEqual(self.fake.extra_counts[name], occurrence)
        self.assertEqual(self.fake.closed()[-3:], [("CloseHandle", 103),
                         ("CloseServiceHandle", 102), ("CloseServiceHandle", 101)])
    return run


for name in ("QueryFullProcessImageNameW", "CreateFileW", "GetFileInformationByHandleEx",
             "GetFinalPathNameByHandleW", "OpenProcessToken", "GetTokenInformation"):
    for label, value in (("zero", 0), ("bool", True), ("exception", RuntimeError("SECRET"))):
        setattr(PrerequisiteTests, "test_fault_" + name + "_" + label, fault_case(name, 1, value))


def file_state(kind, field, value):
    def run(self):
        occurrence = {18: 1, 9: 2, 1: 3}[kind]
        def changed(*args):
            self.fake.GetFileInformationByHandleEx(*args)
            cls = {18: subject.FILE_ID_INFO, 9: subject.FILE_ATTRIBUTE_TAG_INFO,
                   1: subject.FILE_STANDARD_INFO}[kind]
            setattr(C.cast(args[2], C.POINTER(cls)).contents, field, value)
            return 1
        self.fake.overrides[("GetFileInformationByHandleEx", occurrence)] = changed
        self.deny()
    return run


for kind, field, value in ((9, "attributes", 0x400), (9, "attributes", 0x10),
                           (9, "tag", 1), (1, "links", 2), (1, "links", 0),
                           (1, "delete_pending", 1), (1, "directory", 1),
                           (1, "size", 0), (18, "volume", 0)):
    setattr(PrerequisiteTests, f"test_file_{field}_{value}", file_state(kind, field, value))


if __name__ == "__main__":
    unittest.main()
