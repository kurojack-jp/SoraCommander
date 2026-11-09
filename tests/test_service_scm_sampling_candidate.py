"""B04 SCM sampler injection-only tests: zero Win32/SCM/pipe operations."""
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from sora_commander_next.offline_b04_service_identity_probe import SERVICE_NAME
from sora_commander_next.service_scm_sampling_candidate import (
    ScmBackend, ScmSamplingError, sample_scm_configuration,
    SC_MANAGER_CONNECT, SERVICE_QUERY_CONFIG,
)


class FakeBackend(ScmBackend):
    def __init__(self):
        self.events = []
        self.primary = ('"C:\\Protected\\ledger.exe"', "NT AUTHORITY\\LocalService",
                        0x10, 3, 1)
        self.sid_type = 3
        self.change = False
        self.fail_at = None
        self.failed_close = None
        self.scm = 100
        self.service = 200
        self.config_reads = 0

    def step(self, name):
        self.events.append(name)
        if self.fail_at == name:
            raise OSError("synthetic backend failure")

    def open_scm(self, rights):
        self.step(("scm", rights))
        return self.scm

    def open_service(self, scm, name, rights):
        self.step(("service", scm, name, rights))
        return self.service

    def query_config(self, handle):
        self.step(("config", handle))
        self.config_reads += 1
        if self.change and self.config_reads == 2:
            return ('"C:\\Untrusted\\other.exe"', *self.primary[1:])
        return self.primary

    def query_sid_type(self, handle):
        self.step(("sid", handle))
        return self.sid_type

    def close(self, handle):
        self.step(("close", handle))
        if self.failed_close == handle:
            return False
        return True


class ScmSamplerTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.clock_tick = 100

    def clock(self):
        self.clock_tick += 1
        return self.clock_tick

    def read(self, **options):
        return sample_scm_configuration(_backend=self.backend,
            _clock_ms=options.pop("clock", self.clock), **options)

    def test_same_service_handle_two_config_and_sid_queries(self):
        observed = self.read()
        self.assertEqual(observed.first, observed.last)
        self.assertEqual(observed.first.service_name, SERVICE_NAME)
        self.assertTrue(observed.unchanged)
        self.assertEqual(self.backend.events, [
            ("scm", SC_MANAGER_CONNECT),
            ("service", 100, SERVICE_NAME, SERVICE_QUERY_CONFIG),
            ("config", 200), ("sid", 200), ("config", 200), ("sid", 200),
            ("close", 200), ("close", 100)])

    def test_no_service_authority_fields_even_when_perfect(self):
        obs = self.read()
        self.assertEqual(obs.authentication, "NOT_AUTHENTICATED")
        self.assertEqual(obs.admission, "UNAVAILABLE")
        for name in ("native_service_handle_verified","connected_client_pipe_bound",
                     "publisher_pinned","installed_release_verified",
                     "service_process_retained"):
            self.assertIs(getattr(obs, name), False)
        self.assertIn("WINVERIFYTRUST_AND_INDEPENDENT_PUBLISHER_PIN_ABSENT", obs.missing_evidence)

    def test_observation_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self.read().authentication = "AUTHENTICATED"

    def test_cannot_inject_trust_flag(self):
        with self.assertRaises(TypeError):
            self.read(trusted=True)

    def test_no_backend_never_opens_native_dll(self):
        with patch("ctypes.WinDLL", create=True) as load:
            with self.assertRaises(ScmSamplingError) as got:
                sample_scm_configuration()
            self.assertEqual(got.exception.code, "NATIVE_BACKEND_NOT_ACCEPTED")
            load.assert_not_called()

    def test_timeout_strict_types_and_bounds(self):
        for n in (0, 3001, True, False, -5, 1.0, "10"):
            with self.subTest(timeout=n), self.assertRaises(ScmSamplingError):
                self.read(timeout_ms=n)
        self.assertEqual(self.backend.events, [])

    def test_clock_rollback_after_manager_open(self):
        readings = iter((100, 99))
        with self.assertRaises(ScmSamplingError) as got:
            self.read(clock=lambda: next(readings))
        self.assertEqual(got.exception.code, "CLOCK_ROLLBACK")
        self.assertEqual(self.backend.events[-1], ("close", 100))

    def test_timeout_after_manager_open_closes_handle(self):
        readings = iter((100, 101))
        with self.assertRaises(ScmSamplingError) as got:
            self.read(clock=lambda: next(readings), timeout_ms=1)
        self.assertEqual(got.exception.code, "SCM_READ_DEADLINE")
        self.assertEqual(self.backend.events, [("scm", 1), ("close", 100)])

    def test_late_after_final_close_never_returns_record(self):
        values = iter((100, 101, 102, 103, 104, 105))
        with self.assertRaises(ScmSamplingError) as got:
            self.read(clock=lambda: next(values), timeout_ms=5)
        self.assertEqual(got.exception.code, "SCM_READ_DEADLINE")
        self.assertEqual(self.backend.events[-2:], [("close", 200), ("close", 100)])

    def test_changed_record_fails_and_closes(self):
        self.backend.change = True
        with self.assertRaises(ScmSamplingError) as got:
            self.read()
        self.assertEqual(got.exception.code, "SCM_CONFIG_CHANGED")
        self.assertEqual(self.backend.events[-2:], [("close", 200), ("close", 100)])

    def test_open_manager_invalid_handles_rejected(self):
        for value in (0, -1, True, "1", 1<<64):
            with self.subTest(value=value):
                b = FakeBackend(); b.scm = value
                with self.assertRaises(ScmSamplingError):
                    sample_scm_configuration(_backend=b, _clock_ms=self.clock)
                self.assertEqual([e for e in b.events if e[0] == "close"], [])

    def test_open_service_invalid_handles_manager_still_closed(self):
        for value in (0, -1, True, "2", 1<<64):
            with self.subTest(value=value):
                b = FakeBackend(); b.service = value
                with self.assertRaises(ScmSamplingError):
                    sample_scm_configuration(_backend=b, _clock_ms=self.clock)
                self.assertEqual(b.events[-1], ("close", 100))

    def test_aliased_handles_refused_without_double_close(self):
        self.backend.service = 100
        with self.assertRaises(ScmSamplingError) as got:
            self.read()
        self.assertEqual(got.exception.code, "ALISED_SCM_HANDLE")
        self.assertEqual(self.backend.events[-1], ("close", 100))
        self.assertEqual(self.backend.events.count(("close", 100)), 1)

    def test_open_service_exception_manager_always_closed(self):
        self.backend.fail_at = ("service", 100, SERVICE_NAME, 1)
        with self.assertRaises(OSError):
            self.read()
        self.assertEqual(self.backend.events[-1], ("close", 100))

    def test_query_exception_both_handles_closed(self):
        self.backend.fail_at = ("sid", 200)
        with self.assertRaises(OSError):
            self.read()
        self.assertEqual(self.backend.events[-2:], [("close", 200), ("close", 100)])

    def test_invalid_primary_tuple_fails_closed(self):
        for primary in (None, [], ("a",), ("a","b",16,3,True),
                        ("a","b",16,3,-1)):
            with self.subTest(primary=primary):
                b = FakeBackend(); b.primary=primary
                with self.assertRaises(ScmSamplingError):
                    sample_scm_configuration(_backend=b, _clock_ms=self.clock)
                self.assertEqual(b.events[-2:], [("close", 200), ("close", 100)])

    def test_invalid_sid_type_fails_closed(self):
        for sid in (True, -1, 1<<32, "3"):
            with self.subTest(sid=sid):
                b=FakeBackend();b.sid_type=sid
                with self.assertRaises(ScmSamplingError):
                    sample_scm_configuration(_backend=b, _clock_ms=self.clock)
                self.assertEqual(b.events[-2:], [("close", 200), ("close", 100)])

    def test_uncertain_service_close_does_not_grant_observation(self):
        self.backend.failed_close=200
        with self.assertRaises(ScmSamplingError) as got:
            self.read()
        self.assertEqual(got.exception.code, "SCM_HANDLE_CLEANUP_UNKNOWN")
        self.assertEqual(got.exception.cleanup, ("CLOSE_UNCONFIRMED",))
        self.assertEqual(self.backend.events[-1], ("close", 100))

    def test_failed_close_overrides_partial_success_and_failure(self):
        self.backend.failed_close=100
        self.backend.fail_at=("sid",200)
        with self.assertRaises(ScmSamplingError) as got:
            self.read()
        self.assertEqual(got.exception.code, "SCM_HANDLE_CLEANUP_UNKNOWN")

    def test_close_exception_still_attempts_remaining_handle(self):
        self.backend.fail_at=("close",200)
        with self.assertRaises(ScmSamplingError) as got:
            self.read()
        self.assertEqual(got.exception.cleanup, ("CLOSE_FAILED",))
        self.assertEqual(self.backend.events[-1], ("close",100))


class ScmIntegrationTests(unittest.TestCase):
    """The new two-snapshot seam feeds existing B04 comparison, never trust."""
    def setUp(self):
        from sora_commander_next.service_auth_candidate import ReleasePolicyCandidate
        from sora_commander_next.offline_b04_service_identity_probe import ServiceProcessObservation
        from sora_commander_next.service_native_prerequisites import NativePrerequisites
        self.policy = ReleasePolicyCandidate(
            "a"*32, "b"*32, 1, r"C:\Protected\ledger.exe", "c"*64,
            "S-1-5-80-1-2-3-4-5")
        self.native = NativePrerequisites(
            ServiceProcessObservation(SERVICE_NAME, 42, 100, 1, 2),
            self.policy.image_path, (1, "d"*32, 100), "S-1-5-19",
            ((self.policy.service_sid, 4),), ((self.policy.service_sid, 0),), 1)

    def test_collected_records_pass_data_comparison_but_never_authenticate(self):
        from sora_commander_next.service_auth_candidate import compare_authentication_prerequisites
        b = FakeBackend()
        samples = sample_scm_configuration(_backend=b, _clock_ms=lambda: 100)
        result = compare_authentication_prerequisites(
            self.policy, samples.first, samples.last, self.native)
        self.assertTrue(result.configuration_matches)
        self.assertEqual(result.authentication, "NOT_AUTHENTICATED")
        self.assertEqual(result.admission, "UNAVAILABLE")
        self.assertTrue(any("SCM_CONFIG_SAMPLES_NOT_BOUND" in v for v in result.missing_evidence))

    def test_different_policy_pin_results_in_mismatch(self):
        from dataclasses import replace
        from sora_commander_next.service_auth_candidate import compare_authentication_prerequisites
        samples = sample_scm_configuration(_backend=FakeBackend(), _clock_ms=lambda: 100)
        policy = replace(self.policy, image_path=r"E:\Program Files\ledger.exe")
        result = compare_authentication_prerequisites(
            policy, samples.first, samples.last, self.native)
        self.assertFalse(result.configuration_matches)
        self.assertIn("INSTALLED_CONFIGURATION_MISMATCH", result.mismatches)

    def test_injected_config_cannot_be_claimed_as_native_verified(self):
        observed = sample_scm_configuration(_backend=FakeBackend(), _clock_ms=lambda: 100)
        self.assertFalse(observed.native_service_handle_verified)
        self.assertFalse(observed.publisher_pinned)
        with self.assertRaises(TypeError):
            sample_scm_configuration(_backend=FakeBackend(), native_verified=True)


if __name__ == "__main__":
    unittest.main()
