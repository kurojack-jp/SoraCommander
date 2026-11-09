"""DEV_ONLY / DATA_ONLY synthetic composition; no live identity or execution proof.

Only record constructors, dataclass utilities and the existing pure comparator
are used. Pipe/SCM records are juxtaposed, never promoted into native evidence.
"""
from dataclasses import FrozenInstanceError, fields, replace
import unittest

from sora_commander_next.offline_b04_service_identity_probe import (
    ServiceProcessObservation,
)
from sora_commander_next.service_auth_candidate import (
    ReleasePolicyCandidate, ServiceConfigurationRecord,
    compare_authentication_prerequisites,
)
from sora_commander_next.service_native_prerequisites import NativePrerequisites
from sora_commander_next.service_pipe_handle_role_candidate import ServerEndPipeSnapshot
from sora_commander_next.service_scm_retained_bridge_candidate import (
    RetainedScmProcessCandidate,
)


class B04ServiceIdentityCompositionDataOnlyTests(unittest.TestCase):
    def setUp(self):
        # SYNTHETIC constants, not a service installation or native observation.
        self.policy = ReleasePolicyCandidate(
            "1" * 32, "2" * 32, 1, r"C:\Protected\ledger.exe",
            "3" * 64, "S-1-5-80-1-2-3-4-5",
        )
        self.config = ServiceConfigurationRecord(
            "SoraCommanderNextProtectedLedger", '"C:\\Protected\\ledger.exe"',
            "NT AUTHORITY\\LocalService", 16, 3, 1, 3,
        )
        self.process = ServiceProcessObservation(
            "SoraCommanderNextProtectedLedger", 42, 100, 1, 2,
        )
        self.native = NativePrerequisites(
            self.process, self.policy.image_path, (1, "a" * 32, 100),
            "S-1-5-19", (("S-1-5-80-1-2-3-4-5", 4),),
            (("S-1-5-80-1-2-3-4-5", 0),), 1,
        )
        self.scm = RetainedScmProcessCandidate(
            self.process, self.config, self.config,
        )
        self.pipe = ServerEndPipeSnapshot(42, 43, 1, "SERVER", True, 0)

    def compare(self, *, policy=None, scm=None, native=None):
        scm = self.scm if scm is None else scm
        return compare_authentication_prerequisites(
            self.policy if policy is None else policy,
            scm.initial, scm.final, self.native if native is None else native,
        )

    def assert_untrusted_composition(self, result, *, scm=None, pipe=None, native=None):
        scm = self.scm if scm is None else scm
        pipe = self.pipe if pipe is None else pipe
        native = self.native if native is None else native
        for record in (result, native, scm, pipe):
            self.assertEqual(record.authentication, "NOT_AUTHENTICATED")
            self.assertEqual(record.admission, "UNAVAILABLE")
        self.assertIn("SCM_CONFIG_SAMPLES_NOT_BOUND_TO_RETAINED_SERVICE_HANDLE",
                      result.missing_evidence)
        self.assertIn("POLICY_PROVENANCE_AND_IMAGE_DIGEST_NOT_OS_VERIFIED",
                      result.missing_evidence)
        self.assertTrue({
            "CLIENT_HANDLE_PID_CONTRACT_NOT_ACCEPTED",
            "CONNECTION_EPOCH_AND_LIFETIME_NOT_OS_VERIFIED",
            "WINVERIFYTRUST_AND_PROTECTED_RELEASE_BYTES_NOT_IMPLEMENTED",
            "PIPE_DACL_SQOS_LOCALITY_UNVERIFIED",
            "RETAINED_PEER_IDENTITY_THROUGH_REPLY_NOT_IMPLEMENTED",
            "INDEPENDENT_INSTALLER_PUBLISHER_PIN_AND_RELEASE_GENERATION_UNKNOWN",
        }.issubset(result.missing_evidence))
        self.assertIn("CLIENT_PIPE_HANDLE_SERVICE_PID_NOT_OS_BOUND",
                      scm.missing_evidence)
        self.assertIn("SCM_PROCESS_LIFETIME_NOT_LINKED_TO_SERVER_CREATED_PIPE_HANDLE",
                      pipe.missing_evidence)
        self.assertFalse(scm.connected_pipe_identity_verified)
        self.assertFalse(pipe.connected_client_server_identity_verified)

    def test_all_matching_synthetic_components_still_have_no_authority(self):
        self.assertEqual(self.scm.process.pid, 42)
        self.assertEqual(self.pipe.server_pid, 42)
        self.assertEqual(self.native.connection_epoch, 1)
        self.assertEqual(self.pipe.connection_epoch, 1)
        self.assertEqual(self.process.status, "SCM_PROCESS_SNAPSHOT_ONLY")
        result = self.compare()
        self.assertTrue(result.configuration_matches)
        self.assertEqual(result.mismatches, ())
        self.assert_untrusted_composition(result)

    def test_fabricated_client_role_and_matching_pid_cannot_authorize(self):
        pipe = replace(self.pipe, handle_role="CLIENT")
        self.assertEqual(pipe.server_pid, 42)
        self.assertTrue(pipe.server_pid_matches_expected_data)
        self.assertFalse(pipe.server_end_from_create_named_pipe_proven)
        self.assertIn("GET_NAMED_PIPE_SERVER_PROCESS_ID_ON_CLIENT_HANDLE_NOT_DOCUMENTED",
                      pipe.missing_evidence)
        self.assert_untrusted_composition(self.compare(), pipe=pipe)

    def test_pipe_pid_mismatch_is_unbound_not_comparator_validation(self):
        pipe = replace(self.pipe, server_pid=99,
                       server_pid_matches_expected_data=False)
        self.assertEqual(pipe.server_pid, 99)
        self.assertEqual(self.native.process.pid, 42)
        result = self.compare()
        # This comparator does not accept a pipe snapshot or compare its PID.
        self.assertTrue(result.configuration_matches)
        self.assert_untrusted_composition(result, pipe=pipe)

    def test_retained_process_mismatch_does_not_bind_native_snapshot(self):
        scm = replace(self.scm, process=replace(self.process, pid=99))
        self.assertEqual(scm.process.pid, 99)
        self.assertEqual(self.native.process.pid, 42)
        result = self.compare(scm=scm)
        self.assertTrue(result.configuration_matches)
        self.assert_untrusted_composition(result, scm=scm)

    def test_equal_epoch_values_do_not_prove_freshness(self):
        # Reuse old constants: equality provides no clock or lifetime proof.
        result = self.compare()
        self.assertEqual(self.native.connection_epoch, 1)
        self.assertEqual(self.pipe.connection_epoch, 1)
        self.assertFalse(self.scm.image_and_config_live_epoch_verified)
        self.assertIn("CONNECTION_EPOCH_AND_LIFETIME_NOT_OS_VERIFIED",
                      self.native.missing_evidence)
        self.assertIn("PERSISTENT_HANDLE_LIFETIME_THROUGH_AUTHENTICATED_REPLY_ABSENT",
                      self.scm.missing_evidence)
        self.assert_untrusted_composition(result)

    def test_initial_final_configuration_change_denies_match(self):
        scm = replace(self.scm, final=replace(self.config, start_type=2))
        result = self.compare(scm=scm)
        self.assertFalse(result.configuration_matches)
        self.assertEqual(result.mismatches, ("CONFIGURATION_CHANGED",))
        self.assert_untrusted_composition(result, scm=scm)

    def test_equal_configuration_samples_cannot_exclude_aba(self):
        # No intermediate observation exists in the candidate record.
        scm = replace(self.scm, final=replace(self.config))
        result = self.compare(scm=scm)
        self.assertTrue(result.configuration_matches)
        self.assertIn("CONFIG_SAMPLING_ABA_CANNOT_BE_EXCLUDED", scm.missing_evidence)
        self.assert_untrusted_composition(result, scm=scm)

    def test_sid_mismatch_despite_matching_scm_and_pipe(self):
        native = replace(self.native,
                         service_groups=(("S-1-5-80-9-8-7-6-5", 4),))
        result = self.compare(native=native)
        self.assertFalse(result.configuration_matches)
        self.assertEqual(result.mismatches, ("REGISTERED_SERVICE_SID_MISMATCH",))
        self.assert_untrusted_composition(result, native=native)

    def test_deny_only_and_restricted_sid_mismatch_do_not_compose(self):
        for changes in (
            {"service_groups": (("S-1-5-80-1-2-3-4-5", 0x14),)},
            {"restricted_service_groups": (("S-1-5-80-9-8-7-6-5", 0),)},
        ):
            with self.subTest(changes=changes):
                native = replace(self.native, **changes)
                result = self.compare(native=native)
                self.assertFalse(result.configuration_matches)
                self.assertEqual(result.mismatches,
                                 ("REGISTERED_SERVICE_SID_MISMATCH",))
                self.assert_untrusted_composition(result, native=native)

    def test_matching_local_system_config_and_token_still_denied(self):
        config = replace(self.config, account="LocalSystem")
        scm = replace(self.scm, initial=config, final=config)
        native = replace(self.native, token_user="S-1-5-18")
        result = self.compare(scm=scm, native=native)
        self.assertFalse(result.configuration_matches)
        self.assertEqual(result.mismatches, (
            "INSTALLED_CONFIGURATION_MISMATCH", "IMAGE_OR_ACCOUNT_MISMATCH",
        ))
        self.assert_untrusted_composition(result, scm=scm, native=native)

    def test_same_image_with_added_arguments_is_not_installed_match(self):
        config = replace(self.config,
                         binary_command_line='"C:\\Protected\\ledger.exe" --debug')
        scm = replace(self.scm, initial=config, final=config)
        result = self.compare(scm=scm)
        self.assertFalse(result.configuration_matches)
        self.assertEqual(result.mismatches, ("INSTALLED_CONFIGURATION_MISMATCH",))
        self.assert_untrusted_composition(result, scm=scm)

    def test_plausible_digest_is_policy_data_without_independent_verification(self):
        result = self.compare(policy=replace(self.policy, image_sha256="b" * 64))
        self.assertTrue(result.configuration_matches)
        self.assertFalse(self.scm.publisher_trust_verified)
        self.assertFalse(self.pipe.protected_publisher_verified)
        self.assert_untrusted_composition(result)

    def test_replayed_snapshots_and_new_operation_label_do_not_grant(self):
        first = self.compare()
        # Operation labels are local test data, absent from the candidate API.
        for operation_id in ("SYNTHETIC_OPERATION_A", "SYNTHETIC_OPERATION_B"):
            with self.subTest(operation_id=operation_id):
                replay = self.compare()
                self.assertEqual(replay, first)
                self.assert_untrusted_composition(replay)
        with self.assertRaises(TypeError):
            compare_authentication_prerequisites(
                self.policy, self.config, self.config, self.native,
                operation_id="SYNTHETIC_OPERATION_B",
            )

    def test_changed_installation_generation_is_not_a_grant(self):
        result = self.compare(policy=replace(
            self.policy, installation_id="4" * 32, generation="5" * 32,
            release_sequence=2,
        ))
        self.assertEqual(result.policy_identity, ("4" * 32, "5" * 32, 2))
        self.assertTrue(result.configuration_matches)
        self.assertFalse(self.scm.protected_install_verified)
        self.assert_untrusted_composition(result)

    def test_all_composed_records_are_frozen_without_authority_switches(self):
        for record in (self.policy, self.config, self.process, self.native,
                       self.scm, self.pipe, self.compare()):
            with self.subTest(record=type(record).__name__):
                names = {field.name for field in fields(record)}
                self.assertTrue(names.isdisjoint({"trusted", "authorize"}))
                with self.assertRaises(FrozenInstanceError):
                    setattr(record, fields(record)[0].name, None)
        for keyword in ("trusted", "authorize"):
            with self.subTest(keyword=keyword), self.assertRaises(TypeError):
                compare_authentication_prerequisites(
                    self.policy, self.config, self.config, self.native,
                    **{keyword: True},
                )

    def test_missing_native_proofs_and_execution_capability_remain_absent(self):
        result = self.compare()
        self.assertFalse(self.scm.native_handle_cleanup_verified)
        self.assertFalse(self.pipe.pipe_security_descriptor_verified)
        self.assertFalse(self.pipe.peer_owner_token_verified)
        self.assertEqual(self.process.process_lifetime_until_reply, "UNVERIFIED")
        self.assertEqual(self.process.protected_image_publisher_pin_trust, "UNVERIFIED")
        self.assertIn("WIN32_NATIVE_BACKEND_NOT_ACCEPTED", self.scm.missing_evidence)
        self.assertIn("PIPE_DACL_SQOS_REJECT_REMOTE_AND_TOKEN_NOT_OS_VERIFIED",
                      self.pipe.missing_evidence)
        for record in (result, self.native, self.scm, self.pipe):
            for capability in ("execute", "launch", "authorize", "install", "start"):
                self.assertFalse(hasattr(record, capability))
        self.assert_untrusted_composition(result)

    def test_forged_fresh_connection_epoch_still_lacks_identity(self):
        # Epoch is caller-provided data until bound to a real authenticated channel.
        for epoch in (1, 2, 2**63 - 1):
            with self.subTest(epoch=epoch):
                native = replace(self.native, connection_epoch=epoch)
                result = self.compare(native=native)
                self.assertTrue(result.configuration_matches)
                self.assert_untrusted_composition(result, native=native)

    def test_replayed_peer_snapshot_with_new_pid_still_has_no_auth(self):
        for pid in (42, 99, 0xffffffff):
            with self.subTest(pid=pid):
                native = replace(self.native, process=replace(self.process, pid=pid))
                result = self.compare(native=native)
                self.assertTrue(result.configuration_matches)
                self.assertEqual(result.authentication, "NOT_AUTHENTICATED")
                self.assertEqual(result.admission, "UNAVAILABLE")

    def test_forged_release_identity_cannot_raise_admission(self):
        for seq, generation in ((1, "2" * 32), (2, "4" * 32), (2**63 - 1, "f" * 32)):
            with self.subTest(seq=seq):
                policy = replace(self.policy, release_sequence=seq, generation=generation)
                result = self.compare(policy=policy)
                self.assertTrue(result.configuration_matches)
                self.assertEqual(result.authentication, "NOT_AUTHENTICATED")
                self.assertEqual(result.admission, "UNAVAILABLE")
                self.assertIn("POLICY_PROVENANCE_AND_IMAGE_DIGEST_NOT_OS_VERIFIED",
                              result.missing_evidence)

    def test_caller_supplied_authentication_and_admission_are_rejected(self):
        # init=False values cannot be accepted through the constructor API.
        from sora_commander_next.service_auth_candidate import AuthenticationPrerequisites
        for keyword, value in (("authentication", "AUTHENTICATED"),
                               ("admission", "SPAWN_ALLOWED"),
                               ("missing_evidence", ())):
            with self.subTest(keyword=keyword):
                with self.assertRaises(TypeError):
                    AuthenticationPrerequisites(True, (), ("1" * 32, "2" * 32, 1),
                                                **{keyword: value})
        self.assert_untrusted_composition(self.compare())
