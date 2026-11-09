"""Data-only wheel inventory completeness; never installs or downloads."""
import unittest
import tempfile
import hashlib
from pathlib import Path
from sora_commander_next.release_wheelhouse import (
    audit_required_distributions, verify_required_wheelhouse,
    WheelhouseMismatch,
)


def row(name):
    return {"filename": name, "sha256": "0"*64, "bytes": 100}


class RequiredWheelAuditTests(unittest.TestCase):
    def test_exact_windows_cp313_pywin32_present_but_not_authorized(self):
        report = audit_required_distributions([
            row("pywin32-310-cp313-cp313-win_amd64.whl")])
        self.assertTrue(report["required_present"])
        self.assertFalse(report["install_permitted"])
        self.assertFalse(report["publisher_authenticated"])

    def test_missing_pywin32_is_reported_not_assumed(self):
        report = audit_required_distributions([
            row("packaging-25.0-py3-none-any.whl")])
        self.assertFalse(report["required_present"])
        self.assertEqual(report["missing"], ["pywin32"])

    def test_wrong_python_abi_and_nonwindows_rejected(self):
        for filename in (
            "pywin32-310-cp312-cp312-win_amd64.whl",
            "pywin32-310-cp313-cp312-win_amd64.whl",
            "pywin32-310-cp313-cp313-manylinux_x86_64.whl",
        ):
            with self.subTest(filename=filename):
                with self.assertRaises(WheelhouseMismatch):
                    audit_required_distributions([row(filename)])

    def test_outdated_pywin32_and_wrong_architecture_denied(self):
        for filename in (
            "pywin32-305-cp313-cp313-win_amd64.whl",
            "pywin32-foo-cp313-cp313-win_amd64.whl",
            "pywin32-310-cp313-cp313-win_arm64.whl",
        ):
            with self.subTest(filename=filename):
                with self.assertRaises(WheelhouseMismatch):
                    audit_required_distributions([row(filename)])
        valid = audit_required_distributions([
            row("pywin32-306-cp313-cp313-win_arm64.whl")],
            target_platform="win_arm64")
        self.assertTrue(valid["required_present"])
        self.assertFalse(valid["install_permitted"])

    def test_explicit_minimum_and_target_constraints(self):
        good = row("pywin32-310-cp313-cp313-win_amd64.whl")
        report = audit_required_distributions([good], minimum_pywin32=310)
        self.assertTrue(report["required_present"])
        for settings in (
            {"minimum_pywin32": 311},
            {"target_platform": "linux"},
            {"target_platform": None},
            {"minimum_pywin32": True},
        ):
            with self.subTest(settings=settings):
                with self.assertRaises(WheelhouseMismatch):
                    audit_required_distributions([good], **settings)

    def test_full_offline_verification_binds_tags_to_same_bytes(self):
        filename = "pywin32-310-cp313-cp313-win_amd64.whl"
        with tempfile.TemporaryDirectory(prefix="next-wheel-audit-") as d:
            wheel = Path(d) / filename
            wheel.write_bytes(b"synthetic fixture, never install")
            payload = wheel.read_bytes()
            expected = [{
                "filename": filename,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }]
            report = verify_required_wheelhouse(d, expected)
            self.assertTrue(report["hashes_match_supplied_inventory"])
            self.assertTrue(report["required_present"])
            self.assertFalse(report["install_permitted"])
            wheel.write_bytes(payload + b"changed")
            with self.assertRaises(WheelhouseMismatch):
                verify_required_wheelhouse(d, expected)
            wheel.unlink()
            other = Path(d) / "packaging-25.0-py3-none-any.whl"
            other.write_bytes(payload)
            missing = [{
                "filename": other.name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }]
            with self.assertRaises(WheelhouseMismatch):
                verify_required_wheelhouse(d, missing)

    def test_invalid_inventory_and_python_target_denied(self):
        for items in ([], [{}], [row("something.txt")]):
            with self.subTest(items=items):
                with self.assertRaises(WheelhouseMismatch):
                    audit_required_distributions(items)
        with self.assertRaises(WheelhouseMismatch):
            audit_required_distributions([row("pywin32-310-cp313-cp313-win_amd64.whl")],
                                         target_python="../../")


if __name__ == "__main__":
    unittest.main()
