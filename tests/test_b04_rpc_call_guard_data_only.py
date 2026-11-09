"""B04 synthetic RPC per-call negative contracts. NEVER service/auth acceptance."""
from dataclasses import fields, replace
import unittest

from offline_b04_rpc_call_guard_model import (
    FrozenExpectation, ClaimedAttributes, ClaimedToken,
    OfflineCall, begin_call, impersonate, inspect_token, revert,
)


class PerCallGuardDataOnlyTests(unittest.TestCase):
    def setUp(self):
        self.expected = FrozenExpectation()
        self.attrs = ClaimedAttributes()
        self.token = ClaimedToken()

    def check_no_authority(self, call):
        self.assertEqual(call.authentication, "NOT_AUTHENTICATED")
        self.assertEqual(call.admission, "UNAVAILABLE")
        self.assertEqual(call.os_acceptance, "NOT_OS_ACCEPTED")
        self.assertEqual(call.product_side_effects, 0)
        return call

    def attrs_ok(self, **updates):
        call = self.check_no_authority(
            begin_call(self.expected, replace(self.attrs, **updates)))
        return call

    def active(self):
        state = self.attrs_ok()
        self.assertEqual(state.state, "ATTRS_OK")
        state = self.check_no_authority(impersonate(state, result_ok=True))
        self.assertEqual(state.state, "IMPERSONATED")
        return state

    def checked(self, **updates):
        state = self.check_no_authority(inspect_token(
            self.active(), self.expected, replace(self.token, **updates)))
        return state

    def test_matching_data_never_os_authenticated(self):
        state = self.checked()
        self.assertEqual(state.state, "TOKEN_CHECKED")
        state = self.check_no_authority(revert(
            state, result_ok=True, same_active_thread=True))
        self.assertTrue(state.structural_only)

    def test_claimed_auth_and_pid_do_not_promote(self):
        state = self.attrs_ok(claimed_authenticated=True, claimed_pid=1234)
        self.assertEqual(state.state, "ATTRS_OK")
        self.check_no_authority(revert(
            self.checked(claimed_os_verified=True), result_ok=True,
            same_active_thread=True))

    def test_wrong_transport(self):
        self.assertEqual(self.attrs_ok(protocol="ncacn_np").state, "DENIED")

    def test_authn_provider_or_level_downgrade(self):
        for updates in ({"authn_service": 0}, {"authn_level": 1},
                        {"authn_service": True}, {"authn_level": "6"}):
            with self.subTest(updates=updates):
                self.assertEqual(self.attrs_ok(**updates).state, "DENIED")

    def test_wrong_interface_or_opnum(self):
        self.assertEqual(self.attrs_ok(interface="other").state, "DENIED")
        self.assertEqual(self.attrs_ok(opnum=1).state, "DENIED")

    def test_cross_generation_or_approval_or_epoch(self):
        for key in ("generation", "approval_digest", "epoch"):
            with self.subTest(field=key):
                self.assertEqual(self.attrs_ok(**{key: "old"}).state, "DENIED")

    def test_null_session(self):
        self.assertEqual(self.attrs_ok(null_session=True).state, "DENIED")
        self.assertEqual(self.attrs_ok(null_session=1).state, "DENIED")

    def test_cancelled_disconnected_and_unknown_call(self):
        for value in ("CANCELLED", "DISCONNECTED", "UNKNOWN", None):
            with self.subTest(value=value):
                self.assertEqual(self.attrs_ok(call_status=value).state, "DENIED")

    def test_impersonation_failure_is_denied_before_token(self):
        state = self.check_no_authority(impersonate(
            self.attrs_ok(), result_ok=False))
        self.assertEqual(state.state, "DENIED")

    def test_missing_token_still_requires_revert(self):
        state = self.check_no_authority(inspect_token(
            self.active(), self.expected, None))
        self.assertEqual(state.state, "REVERT_REQUIRED")
        result = self.check_no_authority(revert(
            state, result_ok=True, same_active_thread=True))
        self.assertEqual(result.state, "DENIED")

    def test_owner_sid_mismatch_requires_revert(self):
        self.assertEqual(self.checked(user_sid="S-1-5-21-99").state,
                         "REVERT_REQUIRED")

    def test_luid_or_session_mismatch_requires_revert(self):
        for updates in ({"logon_luid": (1, 3)}, {"session_id": 99},
                        {"session_id": True}):
            with self.subTest(updates=updates):
                self.assertEqual(self.checked(**updates).state,
                                 "REVERT_REQUIRED")

    def test_unsupported_impersonation_level_requires_revert(self):
        for value in ("SecurityImpersonation", "SecurityAnonymous", None):
            with self.subTest(level=value):
                self.assertEqual(self.checked(impersonation_level=value).state,
                                 "REVERT_REQUIRED")

    def test_revert_success_after_token_mismatch_stays_denied(self):
        mismatched = self.checked(user_sid="S-1-5-21-99")
        result = self.check_no_authority(revert(
            mismatched, result_ok=True, same_active_thread=True))
        self.assertEqual(result.state, "DENIED")
        self.assertFalse(result.structural_only)

    def test_revert_failure_is_fail_stop(self):
        for value in (False, None, 0, "success"):
            with self.subTest(value=value):
                result = self.check_no_authority(revert(
                    self.checked(), result_ok=value, same_active_thread=True))
                self.assertEqual(result.state, "FAIL_STOP_REQUIRED")
                self.assertFalse(result.structural_only)

    def test_wrong_thread_is_fail_stop_even_if_revert_claims_success(self):
        state = self.check_no_authority(revert(
            self.checked(), result_ok=True, same_active_thread=False))
        self.assertEqual(state.state, "FAIL_STOP_REQUIRED")

    def test_revert_without_token_check_stays_denied(self):
        self.assertEqual(self.check_no_authority(revert(
            self.active(), result_ok=True, same_active_thread=True)).state,
            "DENIED")

    def test_out_of_order_calls_never_authorize(self):
        self.assertEqual(self.check_no_authority(impersonate(
            self.attrs_ok(protocol="bad"), result_ok=True)).state, "DENIED")
        self.assertEqual(self.check_no_authority(inspect_token(
            self.attrs_ok(), self.expected, self.token)).state, "DENIED")
        self.assertEqual(self.check_no_authority(revert(
            self.attrs_ok(), result_ok=True, same_active_thread=True)).state,
            "DENIED")

    def test_forged_observation_types_are_rejected(self):
        self.assertEqual(self.check_no_authority(
            begin_call({}, self.attrs)).state, "DENIED")
        self.assertEqual(self.check_no_authority(inspect_token(
            self.active(), {}, self.token)).state, "REVERT_REQUIRED")

    def test_repeat_impersonation_keeps_revert_obligation(self):
        call = self.active()
        new = self.check_no_authority(impersonate(call, result_ok=True))
        self.assertEqual(new.state, "REVERT_REQUIRED")
        self.assertTrue(new.must_revert)
        self.assertEqual(self.check_no_authority(revert(
            new, result_ok=True, same_active_thread=True)).state, "DENIED")

    def test_token_reinspect_never_loses_revert_obligation(self):
        new = self.check_no_authority(inspect_token(
            self.checked(), self.expected, self.token))
        self.assertEqual(new.state, "REVERT_REQUIRED")
        self.assertTrue(new.must_revert)
        self.assertEqual(self.check_no_authority(revert(
            new, result_ok=True, same_active_thread=True)).state, "DENIED")

    def test_failed_revert_latched_across_all_reentry(self):
        dead = self.check_no_authority(revert(
            self.checked(), result_ok=False, same_active_thread=True))
        self.assertEqual(dead.state, "FAIL_STOP_REQUIRED")
        self.assertTrue(dead.must_revert)
        for new in (impersonate(dead, result_ok=True),
                    inspect_token(dead, self.expected, self.token),
                    revert(dead, result_ok=True, same_active_thread=True)):
            result = self.check_no_authority(new)
            self.assertEqual(result.state, "FAIL_STOP_REQUIRED")
            self.assertTrue(result.must_revert)

    def test_offline_call_cannot_be_constructed_as_verified(self):
        from dataclasses import replace as data_replace
        with self.assertRaises(TypeError):
            OfflineCall()
        with self.assertRaises(TypeError):
            OfflineCall(state="REVERTED")
        with self.assertRaises(TypeError):
            OfflineCall(state="TOKEN_CHECKED", admission="SPAWN_ALLOWED")
        with self.assertRaises(TypeError):
            data_replace(self.attrs_ok(), state="REVERTED")

    def test_expected_owner_registration_swap_requires_revert(self):
        active = self.active()
        changed = replace(self.expected, owner_sid="S-1-5-21-99")
        result = self.check_no_authority(inspect_token(active, changed, self.token))
        self.assertEqual(result.state, "REVERT_REQUIRED")
        self.assertTrue(result.must_revert)

    def test_token_luid_element_boolean_or_float_rejected(self):
        for pair in ((True, 2), (1.0, 2), (1, -1), (1, 2**32)):
            with self.subTest(pair=pair):
                result = self.check_no_authority(inspect_token(
                    self.active(), self.expected,
                    replace(self.token, logon_luid=pair)))
                self.assertEqual(result.state, "REVERT_REQUIRED")

    def test_expected_invalid_schema_fails_at_init(self):
        for val in ((True, 2), (1.0, 2), (1, 2**32), None):
            with self.subTest(val=val), self.assertRaises(ValueError):
                replace(self.expected, logon_luid=val)
        with self.assertRaises(ValueError):
            replace(self.expected, session_id=True)

    def test_frozen_security_floor_cannot_be_redefined(self):
        for values in ({"protocol": "ncacn_np"},
                       {"authn_service": 0}, {"authn_level": 1},
                       {"interface": "untrusted-interface"}, {"opnum": 1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(self.expected, **values)
        for values in ({"protocol": "ncacn_np"},
                       {"authn_service": 0}, {"authn_level": 1},
                       {"interface": "untrusted-interface"}, {"opnum": 1}):
            with self.subTest(values=values):
                result = self.check_no_authority(begin_call(
                    self.expected, replace(self.attrs, **values)))
                self.assertEqual(result.state, "DENIED")

    def test_simultaneous_expected_and_claimed_downgrade_denied(self):
        changed = {"protocol": "ncacn_np", "authn_service": 0,
                   "authn_level": 1}
        with self.assertRaises(ValueError):
            replace(self.expected, **changed)
        result = self.check_no_authority(begin_call(
            self.expected, replace(self.attrs, **changed)))
        self.assertEqual(result.state, "DENIED")

    def test_equal_numeric_values_wrong_type_cannot_pass(self):
        for values in ({"authn_service": 10.0}, {"authn_level": 6.0},
                       {"opnum": False}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(self.expected, **values)
            result = self.check_no_authority(begin_call(
                self.expected, replace(self.attrs, **values)))
            self.assertEqual(result.state, "DENIED")

    def test_immutable_status_and_no_product_admission(self):
        state = self.check_no_authority(revert(
            self.checked(), result_ok=True, same_active_thread=True))
        with self.assertRaises(Exception):
            state.admission = "SPAWN_ALLOWED"
        self.assertEqual(state.product_side_effects, 0)
