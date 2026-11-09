"""B04 retained process+configuration bridge: 100% fake Win32 calls."""
from dataclasses import FrozenInstanceError
from unittest.mock import patch
import ctypes
import unittest

from test_offline_b04_service_identity_probe import FakeWin32
from test_service_scm_query_abi_candidate import FakeDll
from sora_commander_next import offline_b04_service_identity_probe as base
from sora_commander_next.service_scm_query_abi_candidate import ExplicitScmQueryBackend
from sora_commander_next.service_scm_retained_bridge_candidate import (
    observe_retained_config_process, RetainedScmProcessCandidate,
)


class RetainedBridgeTests(unittest.TestCase):
    def setUp(self):
        for target in ("ctypes.WinDLL", "subprocess.Popen", "os.system"):
            g=patch(target, side_effect=AssertionError("real OS operation forbidden"), create=True)
            g.start()
            self.addCleanup(g.stop)
        g=patch.object(base, "_cleanup_uncertain", False)
        g.start()
        self.addCleanup(g.stop)
        self.fake = FakeWin32()
        self.fake.change[("OpenServiceW",1)] = lambda scm,name,rights: (
            self._assert_service(scm,name,rights))
        self.query = FakeDll()
        self.backend = ExplicitScmQueryBackend(self.query, self.query.last_error)

    def _assert_service(self,scm,name,rights):
        self.assertEqual((scm,name,rights),(101,base.SERVICE_NAME,0x5))
        return 102

    def read(self, **kw):
        return observe_retained_config_process(
            _calls=self.fake.api, _config_backend=self.backend, **kw)

    def test_retains_service_and_process_until_config_completed(self):
        actual=self.read()
        self.assertIs(type(actual),RetainedScmProcessCandidate)
        self.assertEqual(actual.process.pid,500)
        self.assertEqual(actual.process.created_filetime,0x0123456789abcdef)
        self.assertEqual(actual.initial,actual.final)
        self.assertEqual(self.query.config_count,2)
        opens=self.fake.events
        self.assertEqual(opens[-3:][0][0],"CloseHandle")
        self.assertEqual(self.fake.closed(),[
            ("CloseHandle",103),("CloseServiceHandle",102),("CloseServiceHandle",101)])
        self.assertEqual(self.fake.counts["QueryServiceStatusEx"],2)
        self.assertEqual(self.fake.counts["GetProcessId"],2)
        self.assertEqual(self.fake.counts["GetProcessTimes"],2)
        self.assertEqual(self.fake.counts["WaitForSingleObject"],3)
        self.assertEqual([e[0] for e in self.query.events].count("fill_config"),2)
        self.assertEqual([e[0] for e in self.query.events].count("fill_sid"),2)

    def test_matches_b04_registered_config_but_does_not_authenticate(self):
        out=self.read()
        self.assertEqual(out.initial.binary_command_line,'"C:\\Protected\\ledger.exe"')
        self.assertEqual(out.initial.account,"NT AUTHORITY\\LocalService")
        self.assertEqual(out.admission,"UNAVAILABLE")
        self.assertEqual(out.authentication,"NOT_AUTHENTICATED")
        for flag in ("connected_pipe_identity_verified","publisher_trust_verified",
                     "protected_install_verified","image_and_config_live_epoch_verified"):
            self.assertFalse(getattr(out,flag))
        self.assertIn("CONFIG_IS_NEXT_START_REGISTRY_STATE_NOT_RUNNING_IMAGE",
                      out.missing_evidence)

    def test_no_installed_service_assumed_without_injected_backend(self):
        with self.assertRaises(base.ObservationError) as failure:
            observe_retained_config_process()
        self.assertEqual(failure.exception.code,"NATIVE_BACKEND_NOT_ACCEPTED")
        self.assertEqual(self.fake.events,[])

    def test_config_backend_is_explicit_and_typed(self):
        with self.assertRaises(base.ObservationError) as failure:
            observe_retained_config_process(_calls=self.fake.api)
        self.assertEqual(failure.exception.code,"CONFIG_BACKEND_NOT_ACCEPTED")
        self.assertEqual(self.fake.events,[])

    def test_untrusted_claimed_data_cannot_authorize(self):
        with self.assertRaises(TypeError):
            self.read(authenticated=True)
        result=self.read()
        with self.assertRaises(FrozenInstanceError):
            result.authentication="AUTHENTICATED"

    def test_both_query_rounds_on_same_service_handle(self):
        self.read()
        self.assertEqual({e[1] for e in self.query.events if e[0] in
                          ("probe_config","fill_config","probe_sid","fill_sid")},{102})

    def test_change_config_between_samples_is_denied(self):
        self.query.changed=True
        with self.assertRaises(base.ObservationError) as failure:
            self.read()
        self.assertEqual(failure.exception.code,"SERVICE_CONFIG_CHANGED")
        self.assertEqual(self.fake.closed(),[
            ("CloseHandle",103),("CloseServiceHandle",102),("CloseServiceHandle",101)])

    def test_corrupt_binary_pointer_denied_and_cleanup(self):
        self.query.bad_string="outside"
        with self.assertRaises(base.ObservationError) as failure:
            self.read()
        self.assertEqual(failure.exception.code,"CONFIG_QUERY_FAILED")
        self.assertEqual(self.fake.closed(),[
            ("CloseHandle",103),("CloseServiceHandle",102),("CloseServiceHandle",101)])

    def test_config_read_error_denied_and_cleanup(self):
        self.query.fail_fill=True
        with self.assertRaises(base.ObservationError) as failure:
            self.read()
        self.assertEqual(failure.exception.code,"CONFIG_QUERY_FAILED")
        self.assertEqual(self.fake.closed(),[
            ("CloseHandle",103),("CloseServiceHandle",102),("CloseServiceHandle",101)])

    def test_final_running_pid_changed_after_config_is_denied(self):
        def new_pid(handle,level,buf,size,needed):
            self.fake.QueryServiceStatusEx(handle,level,buf,size,needed)
            ctypes.cast(buf,ctypes.POINTER(base.SERVICE_STATUS_PROCESS)).contents.dwProcessId=501
            return 1
        self.fake.change[("QueryServiceStatusEx",2)]=new_pid
        with self.assertRaises(base.ObservationError) as failure:
            self.read()
        self.assertEqual(failure.exception.code,"SERVICE_CHANGED")
        self.assertEqual(self.fake.closed(),[
            ("CloseHandle",103),("CloseServiceHandle",102),("CloseServiceHandle",101)])

    def test_final_process_exited_after_config_is_denied(self):
        self.fake.change[("WaitForSingleObject",2)]=0
        with self.assertRaises(base.ObservationError) as failure:
            self.read()
        self.assertEqual(failure.exception.code,"PROCESS_EXITED_OR_UNKNOWN")
        self.assertEqual(self.fake.closed(),[
            ("CloseHandle",103),("CloseServiceHandle",102),("CloseServiceHandle",101)])

    def test_failed_handle_close_latches_and_denies(self):
        self.fake.change[("CloseServiceHandle",1)]=0
        with self.assertRaises(base.ObservationError) as failure:
            self.read()
        self.assertEqual(failure.exception.code,"CLEANUP_UNCONFIRMED")
        self.assertTrue(base._cleanup_uncertain)
        with self.assertRaises(base.ObservationError) as again:
            self.read()
        self.assertEqual(again.exception.code,"CLEANUP_UNCONFIRMED")

    def test_timeout_before_unsafe_operation_is_rejected(self):
        with self.assertRaises(base.ObservationError):
            self.read(timeout_ms=0)
        self.assertEqual(self.fake.events,[])

    def test_service_and_process_must_be_alive_at_final_status(self):
        self.fake.change[("QueryServiceStatusEx",2)]=0
        with self.assertRaises(base.ObservationError) as failure:
            self.read()
        self.assertEqual(failure.exception.code,"ACCESS_DENIED")
        self.assertEqual(self.fake.closed(),[
            ("CloseHandle",103),("CloseServiceHandle",102),("CloseServiceHandle",101)])

    def test_public_service_observer_keeps_original_query_status_only(self):
        # The existing production-gated DEV observer must not change rights.
        result=base.observe_service_process(_calls=FakeWin32().api)
        self.assertEqual(result.authentication,"NOT_AUTHENTICATED")

    def test_result_does_not_export_borrowed_handles(self):
        result=self.read()
        self.assertNotIn("service_handle",result.__dataclass_fields__)
        self.assertNotIn("process_handle",result.__dataclass_fields__)
        self.assertTrue(result.backend_reported_cleanup_success)
        self.assertFalse(result.native_handle_cleanup_verified)

    def test_service_config_cannot_be_mistaken_for_process_file_id(self):
        result=self.read()
        self.assertFalse(hasattr(result,"image_file_identity"))
        self.assertFalse(result.image_and_config_live_epoch_verified)
        self.assertFalse(result.publisher_trust_verified)


if __name__=="__main__":
    unittest.main()
