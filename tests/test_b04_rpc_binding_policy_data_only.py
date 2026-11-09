"""No actual ncalrpc, service, token, or trust checks: reject false promotions."""
from dataclasses import replace
import unittest

from offline_b04_rpc_binding_policy import (
    BindingIntent, inspect_intent, WINNT, PKT_PRIVACY,
    MUTUAL_AUTH_REQUEST, LOCAL_MA_HINT,
)


class RpcBindingPolicyDataOnlyTests(unittest.TestCase):
    def setUp(self):
        self.good = BindingIntent()

    def check(self, overrides=None, consistent=False):
        proposal = replace(self.good, **(overrides or {}))
        result = inspect_intent(proposal)
        self.assertEqual(result.configuration_consistent, consistent)
        self.assertEqual(result.authentication, "NOT_AUTHENTICATED")
        self.assertEqual(result.admission, "UNAVAILABLE")
        self.assertEqual(result.acceptance, "NOT_OS_ACCEPTED")
        self.assertIn("PROTECTED_INSTALLER_PIN_AND_PEER_PROCESS_NOT_VERIFIED",
                      result.missing_or_rejected)
        return result

    def test_structural_candidate_is_only_data(self):
        self.check(consistent=True)

    def test_all_trust_claims_cannot_grant_authority(self):
        self.check({"claimed_trusted_installer": True,
                    "claimed_server_identity_verified": True}, consistent=True)

    def test_default_null_security_is_rejected(self):
        self.assertIn("NULL_OR_INVALID_BINDING_SECURITY",
                      self.check({"explicit_security": False}).missing_or_rejected)

    def test_wrong_transport_is_rejected(self):
        self.check({"protocol": "ncacn_ip_tcp"})
        self.check({"protocol": None})

    def test_wrong_security_version(self):
        self.check({"security_version": 2})

    def test_no_or_unknown_authn_service(self):
        for value in (0, -1, "10", True, None):
            with self.subTest(value=value):
                self.check({"authn_service": value})

    def test_downgraded_authn_level(self):
        for value in (0, 1, 2, 5, True):
            with self.subTest(value=value):
                self.check({"authn_level": value})

    def test_no_qos_v5(self):
        self.check({"qos_version": 0})
        self.check({"qos_version": True})

    def test_no_mutual_auth_request(self):
        self.check({"capabilities": LOCAL_MA_HINT})

    def test_unsupported_capability_bits(self):
        self.check({"capabilities": MUTUAL_AUTH_REQUEST | 0x80000000})
        self.check({"capabilities": True})

    def test_impersonate_or_delegate_not_accepted(self):
        for value in (0, 1, 3, 4, True):
            with self.subTest(value=value):
                self.check({"impersonation": value})

    def test_tracking_ambiguity(self):
        self.check({"identity_tracking": 1})

    def test_effective_only_ambiguous(self):
        self.check({"effective_only": False})
        self.check({"effective_only": True})

    def test_localservice_account_not_service_sid(self):
        self.check({"server_sid": "S-1-5-19"})

    def test_missing_or_untrusted_sid_claim(self):
        for value in (None, "", "S-1-5-80-1-foo", "S-1-5-21-42", 42,
                      "S-1-5-80-1", "S-1-5-80-", "S-1-5-80-1-2-3-4-5-6",
                      "S-1-5-80-1-2-3-4-4294967296", "S-1-5-80-1-2-3-4-²",
                      "S-1-5-80-1-2-3-4-05"):
            with self.subTest(value=value):
                r = self.check({"server_sid": value})
                self.assertIn("MISSING_OR_NONCANONICAL_SERVICE_SID",
                              r.missing_or_rejected)

    def test_multiple_server_identity_forms_not_allowed(self):
        r = self.check({"server_principal": "NT AUTHORITY\\LocalService"})
        self.assertIn("SID_AND_PRINCIPAL_MUST_NOT_BE_COMBINED",
                      r.missing_or_rejected)

    def test_no_server_security_descriptor(self):
        self.check({"server_security_descriptor": False})

    def test_matching_proposal_never_proves_actual_owner(self):
        result = self.check(consistent=True)
        self.assertIn("OWNER_TOKEN_AND_CALL_LIFETIME_NOT_VERIFIED",
                      result.missing_or_rejected)

    def test_input_type_rejection(self):
        for value in (None, {}, self.good.__dict__ if hasattr(self.good, "__dict__") else []):
            with self.subTest(value=value), self.assertRaises(TypeError):
                inspect_intent(value)

    def test_service_sid_dword_bounds_are_only_structural(self):
        good = self.check({"server_sid":
                           "S-1-5-80-0-1-2-3-4294967295"}, consistent=True)
        self.assertEqual(good.authentication, "NOT_AUTHENTICATED")
        for value in ("S-1-5-80-0-1-2-3-4294967296",
                      "S-1-5-80-0-1-2-3-9999999999"):
            bad = self.check({"server_sid": value})
            self.assertIn("MISSING_OR_NONCANONICAL_SERVICE_SID",
                          bad.missing_or_rejected)

    def test_mutual_auth_request_without_local_hint_is_still_data_only(self):
        result = self.check({"capabilities": MUTUAL_AUTH_REQUEST},
                            consistent=True)
        self.assertIn("ACTUAL_WINNT_QOS_SID_BEHAVIOR_NOT_OS_ACCEPTED",
                      result.missing_or_rejected)

    def test_negative_capabilities_and_multiple_invalid_fields(self):
        for value in (-1, None, "1"):
            with self.subTest(value=value):
                result = self.check({"capabilities": value})
                self.assertIn("INVALID_OR_MISSING_MUTUAL_AUTH_REQUEST",
                              result.missing_or_rejected)
        result = self.check({"explicit_security": False,
                             "server_sid": "",
                             "authn_service": 0})
        self.assertTrue({"NULL_OR_INVALID_BINDING_SECURITY",
                         "MISSING_OR_NONCANONICAL_SERVICE_SID",
                         "INVALID_OR_UNREVIEWED_AUTHN_SERVICE"
                         }.issubset(result.missing_or_rejected))

    def test_no_disallowed_native_or_product_modules_imported(self):
        from pathlib import Path
        source = (Path(__file__).parent / "offline_b04_rpc_binding_policy.py").read_text(
            encoding="utf-8")
        for name in ("ctypes", "subprocess", "winreg", "RpcBindingCreate(", "SPAWN_ALLOWED"):
            self.assertNotIn(name, source)
