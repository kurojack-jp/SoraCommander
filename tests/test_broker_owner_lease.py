"""Windows kernel lease acceptance plus explicitly injected failure tests.

The only child is a fixed same-project lease-only script in a disposable temp
directory. No jobs, UI, protected paths, IPC, deployment or permanent changes.
"""
import ast
import ctypes
from ctypes import wintypes
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from tests._inherited_test_temp import inherited_test_temp
from tests._test_owner_lease import isolated_owner_lease

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if os.name == "nt":
    from sora_commander_next import broker_owner_lease as lease
    from sora_commander_next import windows_owner as owner


# Fixed code, independent interpreter, real guard and real kernel acquisition.
# Public source path and test-only prefix are passed explicitly; no environment override.
_CHILD = '''import sys
sys.path.insert(0, sys.argv[1])
from sora_commander_next import broker_owner_lease
broker_owner_lease._LEASE_NAME_PREFIX = sys.argv[2]
from sora_commander_next.broker_owner_lease import BrokerOwnerLease, BrokerOwnerLeaseError
candidate = BrokerOwnerLease()
try:
    with candidate:
        print("ACQUIRED:" + candidate._name)
except BrokerOwnerLeaseError:
    print("DENIED:" + candidate._name)
'''


def _sddl_trustee_sids(sddl):
    import win32security

    parsed = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        sddl, win32security.SDDL_REVISION_1)
    dacl = parsed.GetSecurityDescriptorDacl()
    return [win32security.ConvertSidToStringSid(dacl.GetAce(i)[2])
            for i in range(dacl.GetAceCount())]


@unittest.skipUnless(os.name == "nt", "actual Windows required")
class BrokerOwnerLeaseTests(unittest.TestCase):
    def setUp(self):
        # The exact production-name check constructs only; it never enters.
        if self._testMethodName == "test_name_is_exact_real_logon_digest_and_environment_independent":
            self.enterContext(patch.object(lease, "_CreateMutexW",
                                           side_effect=AssertionError("real mutex forbidden")))
            return
        self.lease_prefix = self.enterContext(isolated_owner_lease())

    def candidate(self):
        candidate = lease.BrokerOwnerLease()
        self.addCleanup(candidate.close)
        return candidate

    def assert_available(self):
        with self.candidate():
            pass

    def precreate(self, candidate, sddl):
        descriptor = wintypes.LPVOID()
        size = wintypes.DWORD()
        self.assertTrue(lease._ConvertSDDL(
            sddl, 1, ctypes.byref(descriptor), ctypes.byref(size)))
        try:
            attributes = lease._SecurityAttributes(
                ctypes.sizeof(lease._SecurityAttributes), descriptor, False)
            ctypes.set_last_error(0)
            handle = lease._CreateMutexW(ctypes.byref(attributes), False, candidate._name)
            error = ctypes.get_last_error()
        finally:
            self.assertFalse(lease._LocalFree(descriptor))
        self.assertTrue(handle)
        self.assertEqual(error, 0)
        return handle

    def test_real_owner_acquire_second_same_process_denied_then_reacquire(self):
        first, second = self.candidate(), self.candidate()
        with first:
            # A restricted token may receive ACCESS_DENIED when opening an
            # existing Owner-only DACL. Both paths must reject, without waiting.
            with self.assertRaises(lease.BrokerOwnerLeaseError):
                second.__enter__()
            self.assertIsNotNone(first._handle)
        self.assert_available()

    def test_real_child_denied_while_parent_holds_then_child_acquires(self):
        first = self.candidate()
        with inherited_test_temp() as directory:
            child = Path(directory) / "lease_child.py"
            child.write_text(_CHILD, encoding="utf-8")

            def run_child():
                result = subprocess.run(
                    [sys.executable, "-I", "-S", "-B", str(child), str(ROOT / "src"),
                     self.lease_prefix],
                    cwd=str(directory), capture_output=True, text=True, timeout=10,
                    close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            with first:
                self.assertEqual(run_child(), "DENIED:" + first._name)
            self.assertEqual(run_child(), "ACQUIRED:" + first._name)
        self.assert_available()

    def test_real_unowned_existing_object_is_denied_without_release(self):
        candidate = self.candidate()
        handle = self.precreate(candidate, "D:P(A;;GA;;;" + candidate._identity.sid + ")")
        try:
            with patch.object(lease, "_ReleaseMutex", wraps=lease._ReleaseMutex) as release:
                with self.assertRaises(lease.BrokerOwnerLeaseError):
                    candidate.__enter__()
                release.assert_not_called()
        finally:
            self.assertTrue(lease._CloseHandle(handle))
        self.assert_available()

    def test_real_existing_conflicting_dacl_is_denied(self):
        candidate = self.candidate()
        sddl = "D:P(D;;GA;;;" + candidate._identity.sid + ")(A;;GA;;;SY)(A;;GA;;;BA)"
        handle = self.precreate(candidate, sddl)
        try:
            with self.assertRaises(lease.BrokerOwnerLeaseError):
                candidate.__enter__()
        finally:
            self.assertTrue(lease._CloseHandle(handle))
        self.assert_available()

    def test_real_kernel_dacl_and_noninheritance(self):
        # Independently query the created kernel object, rather than only
        # asserting the SDDL string supplied to CreateMutexW.
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        get_security = adv.GetSecurityInfo
        get_security.restype = wintypes.DWORD
        get_security.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID,
                                wintypes.LPVOID, ctypes.POINTER(wintypes.LPVOID))
        to_sddl = adv.ConvertSecurityDescriptorToStringSecurityDescriptorW
        to_sddl.restype = wintypes.BOOL
        to_sddl.argtypes = (wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                           ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD))
        get_flags = ctypes.WinDLL("kernel32", use_last_error=True).GetHandleInformation
        get_flags.restype = wintypes.BOOL
        get_flags.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        with self.candidate() as candidate:
            descriptor = wintypes.LPVOID()
            self.assertEqual(get_security(candidate._handle, 6, 4, None, None,
                                          None, None, ctypes.byref(descriptor)), 0)
            try:
                text = wintypes.LPWSTR()
                self.assertTrue(to_sddl(descriptor, 1, 4, ctypes.byref(text), None))
                try:
                    prefix, *aces = text.value.split("(")
                    self.assertEqual(prefix, "D:P")
                    self.assertEqual(len(aces), 3)
                    for ace in aces:
                        kind, flags, rights, object_id, inherited_id, _ = ace.rstrip(")").split(";")
                        self.assertEqual((kind, flags, object_id, inherited_id), ("A", "", "", ""))
                        # Object Manager may map GENERIC_ALL to MUTEX_ALL_ACCESS.
                        self.assertIn(rights, ("GA", "0x1f0001"))
                    # Windows may serialize the actual Owner SID as LA for RID 500.
                    self.assertEqual(_sddl_trustee_sids(text.value),
                                     ["S-1-5-18", "S-1-5-32-544", candidate._identity.sid])
                finally:
                    self.assertFalse(lease._LocalFree(ctypes.cast(text, wintypes.HANDLE)))
            finally:
                self.assertFalse(lease._LocalFree(descriptor))
            flags = wintypes.DWORD()
            self.assertTrue(get_flags(candidate._handle, ctypes.byref(flags)))
            self.assertEqual(flags.value & 1, 0)

    def test_local_administrator_alias_matches_numeric_sid(self):
        aliased = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;LA)"
        system, admins, local_admin = _sddl_trustee_sids(aliased)
        self.assertEqual((system, admins), ("S-1-5-18", "S-1-5-32-544"))
        self.assertRegex(local_admin, r"^S-1-5-21-(?:[0-9]+-){3}500$")
        numeric = aliased.replace(";;;LA)", f";;;{local_admin})")
        self.assertEqual(_sddl_trustee_sids(numeric), [system, admins, local_admin])

    def test_no_acquisition_during_construction(self):
        with patch.object(lease, "_CreateMutexW") as create:
            self.candidate()
            create.assert_not_called()

    def test_no_acquisition_at_import(self):
        kernel = Mock(wraps=ctypes.WinDLL("kernel32", use_last_error=True))
        advapi = Mock(wraps=ctypes.WinDLL("advapi32", use_last_error=True))
        with patch.object(ctypes, "WinDLL", side_effect=[kernel, advapi]) as load:
            spec = importlib.util.spec_from_file_location(
                "sora_commander_next._lease_import_test", lease.__file__)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            # Bindings are allowed; invoking CreateMutexW is not.
            self.assertEqual(load.call_count, 2)
            module._CreateMutexW.assert_not_called()
            module._ConvertSDDL.assert_not_called()

    def test_non_windows_import_fails_before_loading_windows(self):
        spec = importlib.util.spec_from_file_location(
            "sora_commander_next._lease_platform_test", lease.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.object(os, "name", "posix"), patch.object(ctypes, "WinDLL") as load:
            with self.assertRaises(ImportError):
                spec.loader.exec_module(module)
            load.assert_not_called()

    def test_name_is_exact_real_logon_digest_and_environment_independent(self):
        candidate = self.candidate()
        identity = owner.current_windows_owner()
        binding = json.dumps([identity.sid, *identity.logon_luid, identity.session_id],
                             separators=(",", ":")).encode("ascii")
        expected = "Local\\SoraCommanderNext.BrokerOwnerLease.v1." + hashlib.sha256(binding).hexdigest()
        self.assertEqual(candidate._name, expected)
        self.assertNotIn(identity.sid, candidate._name)
        with patch.dict(os.environ, {"SORA_BROKER_MUTEX": "Global\\forged"}):
            self.assertEqual(self.candidate()._name, expected)

    def test_test_prefix_is_distinct_and_helper_restores_it(self):
        real_prefix = "Local\\SoraCommanderNext.BrokerOwnerLease.v1."
        outer_prefix = lease._LEASE_NAME_PREFIX
        with isolated_owner_lease() as prefix:
            self.assertNotEqual(prefix, real_prefix)
            self.assertFalse(prefix.startswith(real_prefix))
            self.assertTrue(self.candidate()._name.startswith(prefix))
        self.assertEqual(lease._LEASE_NAME_PREFIX, outer_prefix)

    def test_rejects_caller_supplied_name_sid_and_guard(self):
        for kwargs in ({"name": "Local\\forged"}, {"sid": "S-1-5-18"},
                       {"owner_guard": Mock(spec=owner.OwnerSessionGuard)}):
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(TypeError):
                lease.BrokerOwnerLease(**kwargs)
        with patch.object(lease, "OwnerSessionGuard", return_value=Mock()):
            with self.assertRaises(TypeError):
                lease.BrokerOwnerLease()

    def test_locked_guard_construction_rejects_before_create(self):
        with patch.object(owner, "session_is_unlocked", return_value=False), \
                patch.object(lease, "_CreateMutexW") as create:
            with self.assertRaises(PermissionError):
                lease.BrokerOwnerLease()
            create.assert_not_called()

    def test_locked_at_enter_rejects_before_create_and_is_terminal(self):
        candidate = self.candidate()
        with patch.object(owner, "session_is_unlocked", return_value=False), \
                patch.object(lease, "_CreateMutexW") as create:
            with self.assertRaises(PermissionError):
                candidate.__enter__()
            create.assert_not_called()
        with self.assertRaises(lease.BrokerOwnerLeaseError):
            candidate.__enter__()
        self.assert_available()

    def test_identity_changes_at_enter_reject_before_create(self):
        for field in ("sid", "logon_luid", "session_id", "active"):
            candidate = self.candidate()
            identity = candidate._identity
            value = {"sid": "S-1-5-18", "logon_luid": (0, 0),
                     "session_id": identity.session_id + 1, "active": False}[field]
            with self.subTest(field=field), \
                    patch.object(owner, "current_windows_owner", return_value=replace(identity, **{field: value})), \
                    patch.object(lease, "_CreateMutexW") as create:
                with self.assertRaises(PermissionError):
                    candidate.__enter__()
                create.assert_not_called()

    def test_lock_immediately_after_real_creation_releases_and_closes(self):
        candidate = self.candidate()
        with patch.object(owner, "session_is_unlocked", side_effect=[True, False]), \
                patch.object(lease, "_ReleaseMutex", wraps=lease._ReleaseMutex) as release, \
                patch.object(lease, "_CloseHandle", wraps=lease._CloseHandle) as close:
            with self.assertRaises(PermissionError):
                candidate.__enter__()
            release.assert_called_once()
            close.assert_called_once_with(release.call_args.args[0])
        self.assertIsNone(candidate._handle)
        self.assert_available()

    def test_identity_changes_immediately_after_creation_cleanup(self):
        for field, value in (("sid", "S-1-5-18"), ("logon_luid", (0, 0)),
                             ("session_id", 0), ("active", False)):
            candidate = self.candidate()
            identity = candidate._identity
            with self.subTest(field=field), patch.object(
                    owner, "current_windows_owner", side_effect=[identity, replace(identity, **{field: value})]):
                with self.assertRaises(PermissionError):
                    candidate.__enter__()
            self.assertIsNone(candidate._handle)
            self.assert_available()

    def test_false_guard_verification_fails_closed(self):
        candidate = self.candidate()
        with patch.object(owner.OwnerSessionGuard, "verify", return_value=False), \
                patch.object(lease, "_CreateMutexW") as create:
            with self.assertRaises(lease.BrokerOwnerLeaseError):
                candidate.__enter__()
            create.assert_not_called()

    def test_sddl_failure_does_not_create_mutex(self):
        candidate = self.candidate()
        with patch.object(lease, "_ConvertSDDL", return_value=False), \
                patch.object(lease, "_CreateMutexW") as create:
            with self.assertRaisesRegex(lease.BrokerOwnerLeaseError, "SDDL"):
                candidate.__enter__()
            create.assert_not_called()
        self.assert_available()

    def test_real_invalid_sddl_is_denied_without_mutex(self):
        candidate = self.candidate()
        convert = lease._ConvertSDDL

        def invalid(text, revision, descriptor, size):
            return convert("invalid SDDL", revision, descriptor, size)

        with patch.object(lease, "_ConvertSDDL", side_effect=invalid), \
                patch.object(lease, "_CreateMutexW") as create:
            with self.assertRaises(lease.BrokerOwnerLeaseError):
                candidate.__enter__()
            create.assert_not_called()
        self.assert_available()

    def test_sddl_missing_pointer_is_denied(self):
        candidate = self.candidate()
        with patch.object(lease, "_ConvertSDDL", return_value=True), \
                patch.object(lease, "_CreateMutexW") as create:
            with self.assertRaises(lease.BrokerOwnerLeaseError):
                candidate.__enter__()
            create.assert_not_called()

    def test_mutex_failure_frees_descriptor_and_does_not_release(self):
        candidate = self.candidate()
        with patch.object(lease, "_CreateMutexW", return_value=None), \
                patch.object(lease, "_LocalFree", wraps=lease._LocalFree) as free, \
                patch.object(lease, "_ReleaseMutex") as release, \
                patch.object(lease, "_CloseHandle") as close:
            with self.assertRaises(lease.BrokerOwnerLeaseError):
                candidate.__enter__()
            free.assert_called_once()
            release.assert_not_called()
            close.assert_not_called()
        self.assert_available()

    def test_existing_status_with_returned_handle_closes_without_release(self):
        candidate = self.candidate()
        create = lease._CreateMutexW

        def existing_status(*args):
            handle = create(*args)
            self.assertTrue(handle)
            ctypes.set_last_error(183)
            return handle

        with patch.object(lease, "_CreateMutexW", side_effect=existing_status), \
                patch.object(lease, "_ReleaseMutex") as release, \
                patch.object(lease, "_CloseHandle", wraps=lease._CloseHandle) as close:
            with self.assertRaisesRegex(lease.BrokerOwnerLeaseError, "already exists"):
                candidate.__enter__()
            release.assert_not_called()
            close.assert_called_once()
        self.assert_available()

    def test_unexpected_status_after_new_mutex_cleans_up(self):
        candidate = self.candidate()
        create = lease._CreateMutexW

        def unexpected(*args):
            handle = create(*args)
            ctypes.set_last_error(87)
            return handle

        with patch.object(lease, "_CreateMutexW", side_effect=unexpected):
            with self.assertRaisesRegex(lease.BrokerOwnerLeaseError, "unexpected"):
                candidate.__enter__()
        self.assert_available()

    def test_descriptor_free_failure_closes_created_mutex(self):
        candidate = self.candidate()
        free = lease._LocalFree

        def failed_free(pointer):
            self.assertFalse(free(pointer))  # Actually free memory in this injection.
            return 1

        with patch.object(lease, "_LocalFree", side_effect=failed_free):
            with self.assertRaisesRegex(lease.BrokerOwnerLeaseError, "descriptor free"):
                candidate.__enter__()
        self.assert_available()

    def test_release_failure_closes_handle_and_raises(self):
        candidate = self.candidate()
        candidate.__enter__()
        with patch.object(lease, "_ReleaseMutex", return_value=False), \
                patch.object(lease, "_CloseHandle", wraps=lease._CloseHandle) as close:
            with self.assertRaisesRegex(lease.BrokerOwnerLeaseError, "release"):
                candidate.close()
            close.assert_called_once()
        self.assertIsNone(candidate._handle)
        candidate.close()
        self.assert_available()

    def test_handle_close_failure_is_terminal_and_raises(self):
        candidate = self.candidate()
        candidate.__enter__()
        close = lease._CloseHandle

        def failed_close(handle):
            self.assertTrue(close(handle))  # No real leaked handle in the test.
            return False

        with patch.object(lease, "_CloseHandle", side_effect=failed_close) as mocked:
            with self.assertRaisesRegex(lease.BrokerOwnerLeaseError, "handle close"):
                candidate.close()
            candidate.close()
            mocked.assert_called_once()
        self.assertIsNone(candidate._handle)
        self.assert_available()

    def test_context_exception_closes_and_propagates(self):
        with self.assertRaisesRegex(ValueError, "body"):
            with self.candidate():
                raise ValueError("body")
        self.assert_available()

    def test_idempotent_close_single_use_and_no_handle_reuse(self):
        candidate = self.candidate()
        candidate.__enter__()
        with self.assertRaises(lease.BrokerOwnerLeaseError):
            candidate.__enter__()
        candidate.close()
        with patch.object(lease, "_ReleaseMutex") as release, \
                patch.object(lease, "_CloseHandle") as close:
            candidate.close()
            with self.assertRaises(lease.BrokerOwnerLeaseError):
                candidate.__enter__()
            release.assert_not_called()
            close.assert_not_called()
        self.assert_available()

    def test_close_before_enter_is_terminal(self):
        candidate = self.candidate()
        candidate.close()
        with self.assertRaises(lease.BrokerOwnerLeaseError):
            candidate.__enter__()

    def test_wrong_thread_close_denied_then_owner_thread_can_cleanup(self):
        candidate = self.candidate()
        candidate.__enter__()
        errors = []

        def wrong_thread():
            try:
                candidate.close()
            except lease.BrokerOwnerLeaseError as error:
                errors.append(type(error))

        thread = threading.Thread(target=wrong_thread, daemon=True)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [lease.BrokerOwnerLeaseError])
        self.assertIsNotNone(candidate._handle)
        candidate.close()
        self.assert_available()

    def test_explicit_core_inventory_classification(self):
        from sora_commander_next.release_inventory import MODULES, DEV_ONLY_MODULES
        self.assertIn("broker_owner_lease.py", MODULES)
        self.assertNotIn("broker_owner_lease.py", DEV_ONLY_MODULES)


class LeaseNameSourceTests(unittest.TestCase):
    def test_production_prefix_has_one_literal_definition_and_no_environment_lookup(self):
        source_path = ROOT / "src" / "sora_commander_next" / "broker_owner_lease.py"
        definitions = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if ((isinstance(node, ast.Name) and node.id == "_LEASE_NAME_PREFIX")
                        or (isinstance(node, ast.Attribute) and node.attr == "_LEASE_NAME_PREFIX")):
                    if isinstance(node.ctx, (ast.Store, ast.Del)):
                        definitions.append((path, node.lineno))
        self.assertEqual(len(definitions), 1, definitions)
        self.assertEqual(definitions[0][0], source_path)
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        definition = next(node for node in tree.body
                          if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "_LEASE_NAME_PREFIX"
                                  for target in node.targets))
        self.assertEqual(ast.literal_eval(definition.value),
                         "Local\\SoraCommanderNext.BrokerOwnerLease.v1.")
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)


if __name__ == "__main__":
    unittest.main()
