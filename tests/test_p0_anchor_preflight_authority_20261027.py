"""Offline authorization-label regressions; no protected state or OS operations."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from sora_commander_next.release_anchor_review import AnchorReview, review_anchor
from sora_commander_next.release_preflight import OfflinePreflight, inspect_offline_candidate
from sora_commander_next.release_layout import review_release_layout, DEFAULT_LAYOUT
from sora_commander_next.release_inventory import CandidateIntegrityError
from sora_commander_next.release_trust_policy import review_policy, evaluate_upgrade, TrustPolicyError
from sora_commander_next.product_composition import build_product_host


class OfflineAuthorityLabels(unittest.TestCase):
    def test_publisher_must_be_exact_string_not_a_self_equal_object(self):
        class SelfEqual:
            def __eq__(self, other):
                return True

        policy = review_policy(publisher="publisher", public_key_sha256="a" * 64,
                               minimum_sequence=1, installed_sequence=1)
        for publisher in (SelfEqual(), None, True, b"publisher"):
            with self.subTest(publisher=type(publisher)), self.assertRaises(TrustPolicyError):
                evaluate_upgrade(trusted_policy=policy, received_public_key_sha256="a" * 64,
                                 signed_publisher=publisher, proposed_sequence=2)

    def test_anchor_constructor_never_accepts_permission_or_status(self):
        for name, value in (("deploy_allowed", True), ("remote_allowed", True),
                            ("journal_write_allowed", True), ("status", "READY")):
            with self.subTest(name=name), self.assertRaises(TypeError):
                AnchorReview("a" * 64, 1, **{name: value})

    def test_anchor_replace_cannot_promote_permissions(self):
        anchor = review_anchor(publisher_fingerprint="a" * 64,
                               owner_confirmed_fingerprint="a" * 64,
                               protected_floor=1, observed_floor=1, proposed_sequence=2)
        for name in ("deploy_allowed", "remote_allowed", "journal_write_allowed"):
            with self.subTest(name=name), self.assertRaises(TypeError):
                replace(anchor, **{name: True})
        self.assertEqual(anchor.status, "OWNER_OUT_OF_BAND_EVIDENCE_REQUIRED")

    def test_preflight_constructor_never_accepts_permission(self):
        data = dict(status="offline", content_verified=True, trust_limit="caller key",
                    pending_reviews=())
        for name in ("deployment_authorized", "remote_authorized"):
            with self.subTest(name=name), self.assertRaises(TypeError):
                OfflinePreflight(**data, **{name: True})
        item = OfflinePreflight(**data)
        self.assertFalse(item.deployment_authorized)
        self.assertFalse(item.remote_authorized)
        self.assertFalse(item.launch_permitted)
        with self.assertRaises(TypeError):
            replace(item, deployment_authorized=True)

    def test_product_host_does_not_become_available_with_untrusted_data(self):
        host = build_product_host()
        try:
            result = host.run_command(request_id="1" * 32)
            self.assertEqual(result["status"], "UNAVAILABLE")
        finally:
            host.close()

    @patch("sora_commander_next.release_preflight._check_ancestor_links")
    @patch("sora_commander_next.release_preflight.verify_candidate")
    def test_real_preflight_function_keeps_hold_after_content_match(self, verify, ancestors):
        layout = review_release_layout(**DEFAULT_LAYOUT)
        result = inspect_offline_candidate("synthetic", expected_manifest_sha256="a" * 64, layout=layout)
        ancestors.assert_called_once_with("synthetic")
        verify.assert_called_once_with("synthetic", trusted_manifest_sha256="a" * 64)
        self.assertTrue(result.content_verified)
        self.assertFalse(result.deployment_authorized)
        self.assertFalse(result.remote_authorized)
        self.assertFalse(result.launch_permitted)
        self.assertIn("NOT authenticated", result.trust_limit)

    @patch("sora_commander_next.release_preflight._check_ancestor_links")
    @patch("sora_commander_next.release_preflight.verify_candidate", side_effect=CandidateIntegrityError("tampered"))
    def test_preflight_tampered_content_cannot_create_review(self, verify, ancestors):
        with self.assertRaises(CandidateIntegrityError):
            inspect_offline_candidate("synthetic", expected_manifest_sha256="a" * 64,
                                      layout=review_release_layout(**DEFAULT_LAYOUT))
        verify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
