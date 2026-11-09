"""Data-join negative tests only: not actual service/SCM/IPC/auth."""
from dataclasses import replace
import unittest

from sora_commander_next.offline_b04_service_identity_probe import (
    ServiceProcessObservation,
)
from sora_commander_next.service_auth_candidate import (
    ReleasePolicyCandidate, ServiceConfigurationRecord,
)
from sora_commander_next.service_native_prerequisites import NativePrerequisites
from sora_commander_next.service_scm_retained_bridge_candidate import (
    RetainedScmProcessCandidate,
)
from offline_b04_provenance_join_model import (
    UntrustedRpcPeerClaim, join_untrusted_provenance,
)


class ProvenanceJoinDataOnlyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ReleasePolicyCandidate(
            "1"*32, "2"*32, 1, r"C:\Protected\ledger.exe", "3"*64,
            "S-1-5-80-1-2-3-4-5",
        )
        self.config = ServiceConfigurationRecord(
            "SoraCommanderNextProtectedLedger", '"C:\\Protected\\ledger.exe"',
            "NT AUTHORITY\\LocalService", 16, 3, 1, 3,
        )
        self.process = ServiceProcessObservation(
            "SoraCommanderNextProtectedLedger", 42, 100, 1, 2,
        )
        self.retained = RetainedScmProcessCandidate(
            self.process, self.config, self.config,
        )
        self.native = NativePrerequisites(
            self.process, self.policy.image_path, (1, "a"*32, 100),
            "S-1-5-19", (("S-1-5-80-1-2-3-4-5", 4),),
            (("S-1-5-80-1-2-3-4-5", 0),), 1,
        )
        self.peer = UntrustedRpcPeerClaim(
            self.process.service_name, 42, 100, 1, "1"*32,
            "2"*32, self.policy.service_sid, self.policy.image_path,
            self.policy.image_sha256,
        )

    def assert_no_auth(self, output, matches):
        self.assertEqual(output.data_matches, matches)
        self.assertEqual(output.authentication, "NOT_AUTHENTICATED")
        self.assertEqual(output.admission, "UNAVAILABLE")
        self.assertEqual(output.os_acceptance, "NOT_OS_ACCEPTED")
        self.assertFalse(output.spawn_allowed)
        self.assertIn("LIVE_RPC_PEER_NOT_BOUND_TO_SERVICE_PROCESS",
                      output.missing_evidence)
        self.assertIn("RELEASE_PIN_MUST_ORIGINATE_FROM_PROTECTED_INSTALLER",
                      output.missing_evidence)
        return output

    def join(self, **changes):
        return join_untrusted_provenance(
            changes.get("policy", self.policy),
            changes.get("retained", self.retained),
            changes.get("native", self.native),
            changes.get("peer", self.peer),
        )

    def test_all_synthetic_records_match_but_untrusted(self):
        r = self.assert_no_auth(self.join(), True)
        self.assertEqual(r.mismatches, ())

    def test_peer_claimed_signed_and_authenticated_still_untrusted(self):
        peer = replace(self.peer, claimed_signed=True,
                       claimed_authenticated=True)
        self.assert_no_auth(self.join(peer=peer), True)

    def test_old_comparator_did_not_bind_scm_process_to_native(self):
        retained = replace(self.retained, process=replace(self.process, pid=99))
        r = self.assert_no_auth(self.join(retained=retained), False)
        self.assertIn("SCM_NATIVE_PROCESS_SNAPSHOTS_MISMATCH", r.mismatches)

    def test_process_creation_time_mismatch_detected(self):
        native = replace(
            self.native, process=replace(self.process, created_filetime=99))
        r = self.assert_no_auth(self.join(native=native), False)
        self.assertIn("SCM_NATIVE_PROCESS_SNAPSHOTS_MISMATCH", r.mismatches)

    def test_retained_service_process_name_mismatch_detected(self):
        retained = replace(
            self.retained, process=replace(self.process, service_name="OtherService"))
        r = self.assert_no_auth(self.join(retained=retained), False)
        self.assertIn("SCM_NATIVE_PROCESS_SNAPSHOTS_MISMATCH", r.mismatches)

    def test_rpc_pid_or_birth_mismatch_detected(self):
        for update in ({"pid": 43}, {"pid": 42.0},
                       {"created_filetime": 99}, {"created_filetime": True}):
            with self.subTest(update=update):
                r = self.assert_no_auth(self.join(
                    peer=replace(self.peer, **update)), False)
                self.assertIn("UNTRUSTED_RPC_PID_OR_BIRTH_MISMATCH",
                              r.mismatches)

    def test_rpc_service_name_mismatch_detected(self):
        r = self.assert_no_auth(
            self.join(peer=replace(self.peer, service_name="Impostor")), False)
        self.assertIn("UNTRUSTED_RPC_SERVICE_NAME_MISMATCH", r.mismatches)

    def test_rpc_epoch_mismatch_and_bool_detected(self):
        for v in (0, 2, True, 1.0):
            with self.subTest(epoch=v):
                r = self.assert_no_auth(
                    self.join(peer=replace(self.peer, epoch=v)), False)
                self.assertIn("UNTRUSTED_RPC_EPOCH_MISMATCH", r.mismatches)

    def test_peer_install_generation_sid_path_hash_mismatch(self):
        for attr, value in (
            ("installation_id", "f"*32),
            ("release_generation", "e"*32),
            ("service_sid", "S-1-5-80-9-8-7-6-5"),
            ("image_path", r"C:\Untrusted\ledger.exe"),
            ("image_sha256", "9"*64),
        ):
            with self.subTest(attr=attr):
                r = self.assert_no_auth(
                    self.join(peer=replace(self.peer, **{attr:value})), False)
                self.assertIn("UNTRUSTED_RPC_"+attr.upper()+"_MISMATCH",
                              r.mismatches)

    def test_scm_config_change_detected(self):
        retained = replace(self.retained, final=replace(self.config, sid_type=1))
        r = self.assert_no_auth(self.join(retained=retained), False)
        self.assertIn("CONFIGURATION_CHANGED", r.mismatches)

    def test_native_service_sid_mismatch_detected(self):
        native = replace(
            self.native, service_groups=(("S-1-5-80-9-8-7-6-5", 4),))
        r = self.assert_no_auth(self.join(native=native), False)
        self.assertIn("REGISTERED_SERVICE_SID_MISMATCH", r.mismatches)

    def test_native_image_path_mismatch_detected(self):
        native = replace(self.native, image_path=r"C:\Untrusted\ledger.exe")
        r = self.assert_no_auth(self.join(native=native), False)
        self.assertIn("IMAGE_OR_ACCOUNT_MISMATCH", r.mismatches)

    def test_wrong_record_type_is_not_accepted(self):
        for kw in ({"retained": self.config}, {"native": self.process},
                   {"policy": {}}, {"peer": {}}):
            with self.subTest(kw=kw), self.assertRaises(TypeError):
                self.join(**kw)

    def test_data_match_does_not_prove_rpc_endpoint_image(self):
        r = self.assert_no_auth(self.join(), True)
        self.assertIn("WINVERIFYTRUST_PUBLISHER_AND_MAPPED_IMAGE_NOT_PROVEN",
                      r.missing_evidence)
        self.assertIn("SCM_TO_NATIVE_SAMPLES_NOT_PROVEN_SAME_RETAINED_HANDLE",
                      r.missing_evidence)

    def test_invalid_scm_process_record_types_are_not_consistent(self):
        changes = (
            {"pid": 42.0}, {"pid": True}, {"pid": -1},
            {"created_filetime": 100.0}, {"created_filetime": True},
            {"started_tick_ns": 1.0}, {"finished_tick_ns": True},
            {"finished_tick_ns": 0},
        )
        for change in changes:
            with self.subTest(change=change):
                process = replace(self.process, **change)
                retained = replace(self.retained, process=process)
                result = self.assert_no_auth(
                    self.join(retained=retained), False)
                self.assertIn("INVALID_SCM_PROCESS_RECORD", result.mismatches)

    def test_invalid_native_process_record_types_are_not_consistent(self):
        changes = ({"pid": 42.0}, {"created_filetime": 100.0},
                   {"started_tick_ns": True}, {"finished_tick_ns": 1.5})
        for change in changes:
            with self.subTest(change=change):
                native = replace(
                    self.native, process=replace(self.process, **change))
                result = self.assert_no_auth(
                    self.join(native=native), False)
                self.assertIn("INVALID_NATIVE_PROCESS_RECORD", result.mismatches)

    def test_frozen_output_rejects_assignment_and_status_replacement(self):
        from dataclasses import FrozenInstanceError, replace as data_replace
        result = self.assert_no_auth(self.join(), True)
        with self.assertRaises(FrozenInstanceError):
            result.admission = "SPAWN_ALLOWED"
        # Python 3.13 rejects dataclass init=False fields with TypeError.
        with self.assertRaises(TypeError):
            data_replace(result, authentication="OS_AUTHENTICATED")
        copied = data_replace(result)
        self.assert_no_auth(copied, True)

    def test_no_mutable_authentication_keyword_in_result(self):
        from offline_b04_provenance_join_model import OfflineProvenanceJoin
        with self.assertRaises(TypeError):
            OfflineProvenanceJoin(True, (), (), admission="SPAWN_ALLOWED")
