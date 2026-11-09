from dataclasses import replace, FrozenInstanceError
import unittest

from sora_commander_next.offline_b01_full_binding import ContractError
from sora_commander_next.offline_b04_service_identity_probe import SERVICE_NAME, ServiceProcessObservation
from sora_commander_next.service_native_prerequisites import NativePrerequisites
from sora_commander_next.service_auth_candidate import (
    ReleasePolicyCandidate, ServiceConfigurationRecord, compare_authentication_prerequisites,
)


class ServiceAuthCandidateTests(unittest.TestCase):
    def setUp(self):
        self.sid = "S-1-5-80-1-2-3-4-5"
        self.policy = ReleasePolicyCandidate("1"*32, "2"*32, 1,
            r"C:\Protected\ledger.exe", "3"*64, self.sid)
        self.config = ServiceConfigurationRecord(SERVICE_NAME,
            '"C:\\Protected\\ledger.exe"', "NT AUTHORITY\\LocalService", 16, 3, 1, 3)
        self.native = NativePrerequisites(ServiceProcessObservation(SERVICE_NAME, 42, 100, 1, 2),
            self.policy.image_path, (1, "a"*32, 100), "S-1-5-19",
            ((self.sid, 4),), ((self.sid, 0),), 1)

    def compare(self, **kw):
        return compare_authentication_prerequisites(kw.get("policy", self.policy),
            kw.get("initial", self.config), kw.get("final", self.config),
            kw.get("native", self.native))

    def test_perfect_synthetic_records_never_authenticate(self):
        result = self.compare()
        self.assertTrue(result.configuration_matches)
        self.assertEqual(result.authentication, "NOT_AUTHENTICATED")
        self.assertEqual(result.admission, "UNAVAILABLE")
        self.assertTrue(any("WINVERIFYTRUST" in r for r in result.missing_evidence))
        self.assertTrue(any("CLIENT_HANDLE" in r for r in result.missing_evidence))

    def test_result_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self.compare().authentication = "AUTHENTICATED"

    def test_no_asserted_trusted_argument(self):
        with self.assertRaises(TypeError):
            compare_authentication_prerequisites(self.policy, self.config, self.config,
                self.native, trusted=True)

    def test_policy_rejects_bool_sequence(self):
        with self.assertRaises(ContractError):
            replace(self.policy, release_sequence=True)

    def test_policy_rejects_invalid_service_sid(self):
        for sid in ("S-1-5-19", "S-1-5-80-1", "S-1-5-80-1-2-3-4-4294967296"):
            with self.subTest(sid=sid), self.assertRaises(ContractError):
                replace(self.policy, service_sid=sid)

    def test_policy_rejects_device_and_ambiguous_paths(self):
        for path in (r"\\?\C:\ledger.exe", r"C:\foo\..\ledger.exe", r"C:\foo\NUL.exe"):
            with self.subTest(path=path), self.assertRaises(Exception):
                replace(self.policy, image_path=path)

    def test_configuration_rejects_non_dword(self):
        for value in (True, -1, 2**32, "3"):
            with self.subTest(value=value), self.assertRaises(ContractError):
                replace(self.config, sid_type=value)

    def test_restricted_sid_type_is_three_not_two(self):
        bad = replace(self.config, sid_type=2)
        self.assertFalse(self.compare(initial=bad, final=bad).configuration_matches)

    def test_unquoted_or_added_arguments_rejected(self):
        for value in (self.policy.image_path, self.config.binary_command_line + " --debug"):
            bad = replace(self.config, binary_command_line=value)
            self.assertFalse(self.compare(initial=bad, final=bad).configuration_matches)

    def test_installed_configuration_mismatches(self):
        for field, value in (("account", "LocalSystem"), ("service_name", "Fake"),
                             ("start_type", 2), ("service_type", 32), ("error_control", 0)):
            with self.subTest(field=field):
                bad = replace(self.config, **{field: value})
                self.assertFalse(self.compare(initial=bad, final=bad).configuration_matches)

    def test_changed_configuration_rejected(self):
        result = self.compare(final=replace(self.config, start_type=2))
        self.assertIn("CONFIGURATION_CHANGED", result.mismatches)

    def test_token_service_sid_must_match_pin(self):
        bad = replace(self.native, service_groups=(("S-1-5-80-9-8-7-6-5", 4),))
        self.assertFalse(self.compare(native=bad).configuration_matches)

    def test_deny_only_and_disabled_token_sid_rejected(self):
        for attr in (0, 0x14, True):
            bad = replace(self.native, service_groups=((self.sid, attr),))
            self.assertFalse(self.compare(native=bad).configuration_matches)

    def test_missing_duplicate_and_malformed_groups_rejected(self):
        for groups in ((), ((self.sid, 4), (self.sid, 4)), [self.sid], ((self.sid,),)):
            bad = replace(self.native, restricted_service_groups=groups)
            self.assertFalse(self.compare(native=bad).configuration_matches)

    def test_image_and_account_mismatch_rejected(self):
        for field, value in (("image_path", r"C:\Other\ledger.exe"), ("token_user", "S-1-5-18")):
            self.assertFalse(self.compare(native=replace(self.native, **{field: value})).configuration_matches)

    def test_bool_pid_and_connection_epoch_rejected(self):
        for native in (replace(self.native, connection_epoch=True),
                       replace(self.native, process=replace(self.native.process, pid=True))):
            self.assertFalse(self.compare(native=native).configuration_matches)

    def test_invalid_image_identity_rejected(self):
        for identity in ((True, "a"*32, 100), (1, "0"*32, 100), (1, "a"*32, True), None):
            self.assertFalse(self.compare(native=replace(self.native, image_identity=identity)).configuration_matches)

    def test_exact_record_types_required(self):
        with self.assertRaises(ContractError):
            self.compare(native={"trusted": True})
