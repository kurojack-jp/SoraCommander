"""Ephemeral staging only; real candidates/PEP427 ZIPs, explicit native mocks.

The real Git test uses all production scanners without patches. No executable
is run, installed, signed or modified outside disposable staging. Ed25519 keys
are ephemeral test keys and never independently protected publisher authority.
"""
import base64
import copy
import csv
from dataclasses import FrozenInstanceError, replace
import hashlib
import io
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile

from release_wire_fixture_helpers import canonical_json_wire

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next import release_broker_bom_files_audit as audit
from sora_commander_next import release_broker_bom_receipt as bom
from sora_commander_next import release_wheel_payload_audit as wheel_audit
from sora_commander_next import release_windows_signature_audit as signature_audit
from sora_commander_next.release_inventory import make_candidate, PACKAGE, CandidateIntegrityError
from sora_commander_next.release_layout import DEFAULT_LAYOUT, review_release_layout


def pin(path):
    data = path.read_bytes()  # Test observations only; production uses signed pins.
    return dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))


def build_wheel(path, *, bad_record=False, altered_member=False):
    files = [("demo/__init__.py", b"raise RuntimeError('NEVER IMPORT')\n"),
             ("demo-1.0.dist-info/METADATA", b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"),
             ("demo-1.0.dist-info/WHEEL", b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")]
    rows = [(name, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(), str(len(data)))
            for name, data in files]
    if bad_record:
        rows[0] = (rows[0][0], "sha256=" + "A" * 43, rows[0][2])
    if altered_member:
        files[0] = (files[0][0], b"# altered member without corresponding RECORD\n")
    record = "demo-1.0.dist-info/RECORD"
    rows.append((record, "", ""))
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in files:
            archive.writestr(name, data)
        archive.writestr(record, output.getvalue().encode())


@unittest.skipUnless(sys.platform == "win32", "Windows fixed-drive staging APIs required")
class ArtifactAuditTests(unittest.TestCase):
    def setUp(self):
        # Python 3.13 mkdtemp(mode=0700) creates an owner-only Windows DACL
        # inaccessible to some restricted test tokens. Create a unique staging
        # directory with the parent's ordinary defaults; never edit any ACL.
        parent = Path(tempfile.gettempdir()).absolute()
        self.home = parent / ("next-bom-files-" + uuid4().hex)
        self.home.mkdir()
        def cleanup():
            if self.home.absolute().parent != parent or not self.home.name.startswith("next-bom-files-"):
                raise AssertionError("cleanup escaped ephemeral staging")
            shutil.rmtree(self.home)
        self.addCleanup(cleanup)
        self.candidate = self.home / "candidate"
        self.digest = make_candidate(ROOT / "src" / PACKAGE, self.candidate)
        self.wheels = self.home / "wheels"
        self.native = self.home / "native"
        self.wheels.mkdir()
        self.native.mkdir()
        self.wheel = self.wheels / "demo-1.0-py3-none-any.whl"
        build_wheel(self.wheel)
        (self.native / "DLLs").mkdir()
        for name in ("DLLs/a.pyd", "python.exe", "python313.dll"):
            (self.native / name).write_bytes(b"MZ deterministic UNSIGNED native fixture " + name.encode())
        self.private = Ed25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes_raw()
        self.layout = review_release_layout(**DEFAULT_LAYOUT)
        self.payload = dict(
            format=bom.RECEIPT_FORMAT, product=bom.PRODUCT, publisher="test_publisher",
            sequence=7, candidate_manifest_sha256=self.digest,
            target_python="cp313", target_platform="win_amd64",
            wheels=[dict(filename=self.wheel.name, **pin(self.wheel))],
            native_binaries=[dict(relative_path=name, role="interpreter" if name == "python.exe" else "dependency",
                                  **pin(self.native / name))
                             for name in ("DLLs/a.pyd", "python.exe", "python313.dll")])
        self.receipt = self.sign()

    def sign(self, payload=None, *, validate=True):
        value = self.payload if payload is None else payload
        data = bom.receipt_signing_bytes(value) if validate else bom.DOMAIN + canonical_json_wire(value)
        return canonical_json_wire(dict(format=bom.RECEIPT_FORMAT, payload=value,
                              signature_hex=self.private.sign(data).hex()))

    def run_audit(self, **overrides):
        args = dict(candidate_root=self.candidate, wheelhouse_root=self.wheels,
                    native_root=self.native, receipt_bytes=self.receipt,
                    trusted_ed25519_public_key=self.public, trusted_publisher="test_publisher",
                    minimum_sequence=7, installed_sequence=6, layout=self.layout)
        args.update(overrides)
        return audit.audit_signed_broker_artifacts(**args)

    def mock_native_result(self, path, *, expected_sha256, expected_size):
        # Explicit TEST ONLY substitute for trust, independently check passed pins.
        actual = pin(path)
        if actual != dict(sha256=expected_sha256, bytes=expected_size):
            raise signature_audit.WindowsSignatureAuditError("mock native pin mismatch")
        return SimpleNamespace(sha256=expected_sha256, size=expected_size,
                               windows_chain_policy_accepted=True,
                               winverifytrust_status=0, state_close_status=0)

    def test_explicit_mock_native_full_byte_composition_and_frozen_rights(self):
        with patch.object(audit, "inspect_embedded_signature", side_effect=self.mock_native_result) as scanner:
            result = self.run_audit()
        self.assertEqual([c.args[0].relative_to(self.native).as_posix() for c in scanner.call_args_list],
                         ["DLLs/a.pyd", "python.exe", "python313.dll"])
        for call, expected in zip(scanner.call_args_list, self.payload["native_binaries"]):
            self.assertEqual(call.kwargs, dict(expected_sha256=expected["sha256"], expected_size=expected["bytes"]))
        self.assertEqual(result.scanned_wheel_names, (self.wheel.name,))
        self.assertEqual(result.scanned_native_paths, ("DLLs/a.pyd", "python.exe", "python313.dll"))
        self.assertEqual(result.native_roles, (("DLLs/a.pyd", "dependency"), ("python.exe", "interpreter"), ("python313.dll", "dependency")))
        self.assertEqual((result.wheel_count, result.native_binary_count), (1, 3))
        self.assertEqual(result.publisher, "test_publisher")
        self.assertEqual(result.sequence, 7)
        self.assertEqual(result.verified_public_key_fingerprint, hashlib.sha256(self.public).hexdigest())
        self.assertEqual(result.candidate_manifest_sha256, self.digest)
        self.assertEqual(result.status, "HOLD_LIVE")
        for name in ("candidate_content_verified", "wheel_payloads_verified", "native_signatures_observed"):
            self.assertIs(getattr(result, name), True)
        for name in ("external_trust_anchor_origin_verified", "publisher_pinned", "deployment_authorized",
                     "install_permitted", "launch_permitted", "remote_authorized"):
            self.assertIs(getattr(result, name), False)
            with self.assertRaises((TypeError, ValueError)):
                replace(result, **{name: True})
        with self.assertRaises(FrozenInstanceError):
            result.sequence = 8

    def test_signature_policy_first_no_files_inspected(self):
        for overrides in (
                dict(trusted_ed25519_public_key=Ed25519PrivateKey.generate().public_key().public_bytes_raw()),
                dict(trusted_publisher="wrong_publisher"), dict(minimum_sequence=8),
                dict(installed_sequence=7), dict(installed_sequence=8), dict(receipt_bytes=b"bad")):
            with self.subTest(overrides=overrides), patch.object(audit, "_directory") as directory:
                with self.assertRaises(bom.BrokerBomReceiptError):
                    self.run_audit(**overrides)
                directory.assert_not_called()

    def test_every_wheel_scanned_with_its_own_signed_pin(self):
        other = self.wheels / "extra-1.0-py3-none-any.whl"
        # Keep matching distribution/metadata names in the second wheel.
        original = self.wheel.read_bytes()
        other.write_bytes(original)
        # A build tag gives a distinct canonical filename for the same dist.
        other.rename(self.wheels / "demo-1.0-1-py3-none-any.whl")
        other = self.wheels / "demo-1.0-1-py3-none-any.whl"
        self.payload["wheels"].append(dict(filename=other.name, **pin(other)))
        self.payload["wheels"].sort(key=lambda item: item["filename"].casefold())
        self.receipt = self.sign()
        with patch.object(audit, "audit_wheel_payload", wraps=wheel_audit.audit_wheel_payload) as scanner:
            with patch.object(audit, "inspect_embedded_signature", side_effect=self.mock_native_result):
                result = self.run_audit()
        expected = self.payload["wheels"]
        self.assertEqual(result.scanned_wheel_names, tuple(p["filename"] for p in expected))
        self.assertEqual(result.wheel_count, 2)
        self.assertEqual([call.args[0].name for call in scanner.call_args_list], list(result.scanned_wheel_names))
        self.assertEqual([call.kwargs for call in scanner.call_args_list],
                         [dict(expected_sha256=p["sha256"], expected_size=p["bytes"]) for p in expected])

    def test_mock_fixed_drive_mount_alias_and_handle_reparse_denied(self):
        # Native directory checks only; never replace a production scanner.
        import ctypes
        kernel = ctypes.WinDLL("kernel32.dll", winmode=0x800)
        with patch.object(audit.ctypes, "WinDLL", return_value=kernel):
            with patch.object(kernel, "GetDriveTypeW", return_value=4):
                with self.assertRaisesRegex(audit.BrokerBomFilesAuditError, "fixed drive"):
                    with audit._directory(self.wheels):
                        self.fail("nonfixed root accepted")
            def redirected(handle, buffer, count, flags):
                buffer.value = r"\\?\Z:\redirected"
                return len(buffer.value)
            with patch.object(kernel, "GetFinalPathNameByHandleW", side_effect=redirected):
                with self.assertRaisesRegex(audit.BrokerBomFilesAuditError, "alias"):
                    with audit._directory(self.wheels):
                        self.fail("alias root accepted")
            def reparse(handle, kind, buffer, count):
                buffer[0] = 0x410
                return 1
            with patch.object(kernel, "GetFileInformationByHandleEx", side_effect=reparse):
                with self.assertRaisesRegex(audit.BrokerBomFilesAuditError, "opened directory"):
                    with audit._directory(self.wheels):
                        self.fail("handle reparse accepted")

    def test_missing_root_is_not_created_and_false_candidate_is_rejected(self):
        missing = self.home / "missing"
        with self.assertRaises(FileNotFoundError):
            self.run_audit(wheelhouse_root=missing)
        self.assertFalse(missing.exists())
        with patch.object(audit, "inspect_offline_candidate", return_value=SimpleNamespace(content_verified=False)):
            with self.assertRaisesRegex(audit.BrokerBomFilesAuditError, "candidate scanner"):
                self.run_audit()

    def test_candidate_tamper_after_signing(self):
        with (self.candidate / PACKAGE / "__init__.py").open("ab") as stream:
            stream.write(b"\n# tampered\n")
        with patch.object(audit, "audit_wheel_payload") as scanner:
            with self.assertRaises(CandidateIntegrityError):
                self.run_audit()
            scanner.assert_not_called()

    def test_wheel_tamper_after_signing(self):
        build_wheel(self.wheel, altered_member=True)
        with self.assertRaises(wheel_audit.WheelPayloadAuditError):
            self.run_audit()

    def test_resigned_wheel_bad_record_and_member_still_rejected(self):
        for options in (dict(bad_record=True), dict(altered_member=True)):
            with self.subTest(options=options):
                build_wheel(self.wheel, **options)
                self.payload["wheels"][0].update(pin(self.wheel))
                self.receipt = self.sign()
                with self.assertRaisesRegex(wheel_audit.WheelPayloadAuditError, "RECORD"):
                    self.run_audit()

    def test_native_tamper_after_signing_real_scanner(self):
        path = self.native / "DLLs/a.pyd"
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        with self.assertRaisesRegex(signature_audit.WindowsSignatureAuditError, "SHA256 pin mismatch"):
            self.run_audit()

    def test_wheel_inventory_reads_file_identity_once(self):
        # Each tree snapshot still checks ordinary type, reparse and hardlinks.
        # Do not accidentally duplicate Windows lstat() work per entry.
        original = Path.lstat
        observed = []

        def inspected(path, *args, **kwargs):
            if path == self.wheel:
                observed.append(path)
            return original(path, *args, **kwargs)

        with patch.object(Path, "lstat", inspected), audit.ExitStack() as stack:
            inventory = audit._inventory(self.wheels, native=False, stack=stack)
        self.assertEqual(inventory[0], (self.wheel.name,))
        self.assertEqual(observed, [self.wheel])

    def test_missing_extra_and_unrelated_wheel_entries(self):
        self.wheel.rename(self.wheel.with_suffix(".bak"))
        with self.assertRaises(audit.BrokerBomFilesAuditError):
            self.run_audit()
        self.wheel.with_suffix(".bak").rename(self.wheel)
        for name in ("extra-1.0-py3-none-any.whl", "readme.txt", "subdirectory"):
            path = self.wheels / name
            if name == "subdirectory":
                path.mkdir()
            else:
                path.write_bytes(b"extra")
            with self.subTest(name=name), self.assertRaises(audit.BrokerBomFilesAuditError):
                self.run_audit()
            path.rmdir() if path.is_dir() else path.unlink()
        self.wheel.unlink()
        with self.assertRaisesRegex(audit.BrokerBomFilesAuditError, "signed BOM"):
            self.run_audit()

    def test_native_missing_extra_unrelated_and_empty_subdirectory(self):
        path = self.native / "python313.dll"
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises(audit.BrokerBomFilesAuditError):
            self.run_audit()
        path.write_bytes(original)
        for name in ("extra.exe", "extra.txt", "empty"):
            path = self.native / name
            path.mkdir() if name == "empty" else path.write_bytes(b"extra")
            with self.subTest(name=name), self.assertRaises(audit.BrokerBomFilesAuditError):
                self.run_audit()
            path.rmdir() if path.is_dir() else path.unlink()

    def test_mismatched_signed_wheel_and_native_pins_real_scanners(self):
        for kind in ("wheels", "native_binaries"):
            for field, value in (("sha256", "0" * 64), ("bytes", 1)):
                data = copy.deepcopy(self.payload)
                data[kind][0][field] = value
                with self.subTest(kind=kind, field=field), self.assertRaises(
                        wheel_audit.WheelPayloadAuditError if kind == "wheels" else signature_audit.WindowsSignatureAuditError):
                    self.run_audit(receipt_bytes=self.sign(data))

    def test_signed_duplicate_casefold_traversal_and_roles_denied(self):
        for kind, field in (("wheels", "filename"), ("native_binaries", "relative_path")):
            for name in (self.payload[kind][0][field].upper(), "../python.exe", "x/python.exe:ads"):
                data = copy.deepcopy(self.payload)
                extra = dict(data[kind][0], **{field: name})
                data[kind].append(extra)
                with self.subTest(kind=kind, name=name), self.assertRaises(bom.BrokerBomReceiptError):
                    self.run_audit(receipt_bytes=self.sign(data, validate=False))
        data = copy.deepcopy(self.payload)
        data["native_binaries"][0]["role"] = "interpreter"
        with self.assertRaises(bom.BrokerBomReceiptError):
            self.run_audit(receipt_bytes=self.sign(data, validate=False))

    def test_signed_case_spelling_requires_exact_files_not_count(self):
        data = copy.deepcopy(self.payload)
        data["native_binaries"][0]["relative_path"] = "dlls/a.pyd"
        with self.assertRaisesRegex(audit.BrokerBomFilesAuditError, "signed BOM"):
            self.run_audit(receipt_bytes=self.sign(data))

    def test_path_syntax_and_protected_overlap_rejected_before_read(self):
        for field in ("candidate_root", "wheelhouse_root", "native_root"):
            for value in ("relative", r"C:relative", r"\\server\share", r"\\?\C:\stage",
                          r"C:\stage\..\other", r"C:\stage:ads", r"C:\NUL\stage", r"C:\stage.\other",
                          self.layout.code_root, self.layout.code_root + r"\stage", None):
                with self.subTest(field=field, value=value), patch.object(audit, "_directory") as scanner:
                    with self.assertRaises(audit.BrokerBomFilesAuditError):
                        self.run_audit(**{field: value})
                    scanner.assert_not_called()

    def test_real_hardlinks_rejected_for_wheel_and_each_native(self):
        for source in (self.wheel, self.native / "DLLs/a.pyd", self.native / "python.exe"):
            link = self.home / "hardlink"
            os.link(source, link)
            try:
                with self.subTest(source=source), self.assertRaisesRegex(audit.BrokerBomFilesAuditError, "single-link"):
                    self.run_audit()
            finally:
                link.unlink()

    def test_mock_reparse_root_and_every_ancestor_denied(self):
        original = Path.lstat
        for root in (self.candidate, self.wheels, self.native):
            for target in (root, *root.parents):
                def changed(path, *args, **kwargs):
                    info = original(path, *args, **kwargs)
                    if path == target:
                        return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                    return info
                with self.subTest(target=target), patch.object(Path, "lstat", changed):
                    with self.assertRaisesRegex(audit.BrokerBomFilesAuditError, "nonreparse"):
                        self.run_audit()

    def test_mock_symlink_and_reparse_entries_never_scanned(self):
        original = Path.lstat
        for root, name in ((self.wheels, self.wheel.name), (self.native, "python.exe"), (self.native, "DLLs")):
            target = root / name
            info = original(target)
            for mode, attrs in ((stat.S_IFLNK | 0o777, 0), (info.st_mode, 0x400)):
                def altered(path, *args, **kwargs):
                    if path == target:
                        return SimpleNamespace(st_mode=mode, st_file_attributes=attrs)
                    return original(path, *args, **kwargs)
                entry = SimpleNamespace(name=name)
                with self.subTest(root=root, mode=mode, attrs=attrs), patch.object(
                        audit.os, "scandir") as scan, patch.object(Path, "lstat", altered):
                    scan.return_value.__enter__.return_value = iter([entry])
                    with audit.ExitStack() as stack, self.assertRaisesRegex(
                            audit.BrokerBomFilesAuditError, "reparse/link"):
                        audit._inventory(root, native=root == self.native, stack=stack)

    def test_mock_casefold_entry_collision(self):
        real = self.wheel.lstat()
        entries = [SimpleNamespace(name=n, stat=lambda **kwargs: real)
                   for n in (self.wheel.name, self.wheel.name.upper())]
        with patch.object(audit.os, "scandir") as scan:
            scan.return_value.__enter__.return_value = iter(entries)
            with audit.ExitStack() as stack, self.assertRaisesRegex(audit.BrokerBomFilesAuditError, "casefold"):
                audit._inventory(self.wheels, native=False, stack=stack)

    def test_entry_and_depth_bounds(self):
        for number in range(64):
            (self.wheels / f"extra{number}-1.0-py3-none-any.whl").write_bytes(b"x")
        with self.assertRaisesRegex(audit.BrokerBomFilesAuditError, "bound"):
            self.run_audit()
        for path in self.wheels.iterdir():
            if path != self.wheel:
                path.unlink()
        directory = self.native
        for number in range(audit.MAX_NATIVE_DEPTH + 1):
            directory = directory / str(number)
            directory.mkdir()
        (directory / "deep.dll").write_bytes(b"x")
        with self.assertRaisesRegex(audit.BrokerBomFilesAuditError, "bound"):
            self.run_audit()

    def test_scanner_exceptions_never_swallowed_or_skipped(self):
        for target in ("inspect_offline_candidate", "audit_wheel_payload", "inspect_embedded_signature"):
            error = RuntimeError("explicit deterministic scanner failure")
            with self.subTest(target=target), patch.object(audit, target, side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    self.run_audit()
                self.assertIs(caught.exception, error)

    def test_unsuccessful_scanner_evidence_denied(self):
        for changes in (dict(archive_sha256="0" * 64), dict(archive_size=1), dict(record_coverage_verdict="FAIL")):
            evidence = dict(archive_sha256=self.payload["wheels"][0]["sha256"],
                            archive_size=self.payload["wheels"][0]["bytes"], record_coverage_verdict="PASS")
            evidence.update(changes)
            with self.subTest(changes=changes), patch.object(audit, "audit_wheel_payload", return_value=SimpleNamespace(**evidence)):
                with self.assertRaisesRegex(audit.BrokerBomFilesAuditError, "wheel scanner"):
                    self.run_audit()
        for changes in (dict(sha256="0" * 64), dict(size=1), dict(windows_chain_policy_accepted=False),
                        dict(winverifytrust_status=1), dict(state_close_status=1)):
            def result(path, **kwargs):
                value = self.mock_native_result(path, **kwargs)
                value.__dict__.update(changes)
                return value
            with self.subTest(changes=changes), patch.object(audit, "inspect_embedded_signature", side_effect=result):
                with self.assertRaisesRegex(audit.BrokerBomFilesAuditError, "native scanner"):
                    self.run_audit()

    def test_late_extra_file_rejected(self):
        def scanner(path, **kwargs):
            evidence = self.mock_native_result(path, **kwargs)
            (self.wheels / "late-1.0-py3-none-any.whl").write_bytes(b"late")
            return evidence
        with patch.object(audit, "inspect_embedded_signature", side_effect=scanner):
            with self.assertRaisesRegex(audit.BrokerBomFilesAuditError, "changed during scan"):
                self.run_audit()

    def test_no_evidence_or_callback_injection_api(self):
        for key in ("verified_bom", "wheel_auditor", "native_auditor"):
            with self.subTest(key=key), self.assertRaises(TypeError):
                self.run_audit(**{key: lambda *args, **kwargs: True})

    def test_real_signed_git_all_three_artifact_classes_then_tamper(self):
        git = shutil.which("git")
        if git is None:
            self.skipTest("Git executable unavailable for real embedded-signature fixture")
        source = Path(git)
        observed = pin(source)
        if observed["bytes"] > signature_audit.DEFAULT_MAX_BYTES:
            self.skipTest("Git executable exceeds native inspector default byte bound")
        for path in (self.native / "DLLs/a.pyd", self.native / "python313.dll"):
            path.unlink()
        (self.native / "DLLs").rmdir()
        target = self.native / "python.exe"
        shutil.copyfile(source, target)
        # Installed Git may have legitimate hardlinks; inspect its ordinary,
        # single-link staging COPY before signing the ephemeral test BOM.
        try:
            confirmed = signature_audit.inspect_embedded_signature(
                target, expected_sha256=observed["sha256"], expected_size=observed["bytes"])
        except signature_audit.WindowsSignatureAuditError as exc:
            self.skipTest(f"Git staging copy unsuitable for real offline embedded trust fixture: {exc}")
        self.assertIs(confirmed.windows_chain_policy_accepted, True)
        self.payload["native_binaries"] = [dict(role="interpreter", relative_path="python.exe", **observed)]
        self.receipt = self.sign()
        result = self.run_audit()  # No mocks: signature, candidate, ZIP/RECORD, native WinVerifyTrust.
        self.assertTrue(result.candidate_content_verified)
        self.assertTrue(result.wheel_payloads_verified)
        self.assertTrue(result.native_signatures_observed)
        self.assertEqual(result.scanned_native_paths, ("python.exe",))
        self.assertEqual(result.scanned_wheel_names, (self.wheel.name,))
        self.assertFalse(result.publisher_pinned)
        self.assertFalse(result.install_permitted)
        self.assertEqual(result.status, "HOLD_LIVE")
        data = bytearray(target.read_bytes())
        data[0x40] ^= 1
        target.write_bytes(data)
        with self.assertRaisesRegex(signature_audit.WindowsSignatureAuditError, "SHA256 pin mismatch"):
            self.run_audit()
        self.payload["native_binaries"][0].update(pin(target))
        self.receipt = self.sign()
        with self.assertRaisesRegex(signature_audit.WindowsSignatureAuditError, "WinVerifyTrust rejected"):
            self.run_audit()
        self.assertEqual(pin(source), observed)


if __name__ == "__main__":
    unittest.main()
