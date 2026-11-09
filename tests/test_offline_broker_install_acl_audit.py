"""Read-only protected-foundation ACL audit tests; never modify protected paths.

Only synthetic ACL inputs and GetFileSecurity queries of already-existing
foundation dirs. Does not elevate, install code, launch Broker, or create ACLs.
"""
import os
import unittest
from unittest.mock import patch

from sora_commander_next.offline_broker_install_acl_audit import (
    SYSTEM, ADMINS, USERS, FULL, READ_EXEC,
    classify_code_root_acl, audit_foundation_readonly,
    DirectoryAclObservation,
)


class ProtectedFoundationAuditTests(unittest.TestCase):
    @staticmethod
    def expected():
        return (
            (SYSTEM, True, False, 3, FULL),
            (ADMINS, True, False, 3, FULL),
            (USERS, True, False, 3, READ_EXEC),
        )

    def test_exact_admin_code_root_acl_shape(self):
        self.assertTrue(classify_code_root_acl(
            owner_sid=ADMINS, protected=True, entries=self.expected()))
        self.assertTrue(classify_code_root_acl(
            owner_sid=ADMINS, protected=True,
            entries=tuple(reversed(self.expected()))))

    def test_writable_users_inherited_or_extra_ace_fail_closed(self):
        baseline = self.expected()
        for bad in (
            baseline[:2] + ((USERS, True, False, 3, FULL),),
            baseline[:2] + ((USERS, True, True, 3, READ_EXEC),),
            baseline[:2] + ((USERS, False, False, 3, READ_EXEC),),
            baseline[:2] + ((USERS, True, False, 1, READ_EXEC),),
            baseline + (("S-1-1-0", True, False, 3, FULL),),
            baseline[:2] + (baseline[0],),
            baseline[:2],
        ):
            with self.subTest(bad=bad):
                self.assertFalse(classify_code_root_acl(
                    owner_sid=ADMINS, protected=True, entries=bad))
        for owner, protected in ((USERS, True), (ADMINS, False),
                                 (ADMINS, 1)):
            with self.subTest(owner=owner, protected=protected):
                self.assertFalse(classify_code_root_acl(
                    owner_sid=owner, protected=protected, entries=baseline))

    def test_missing_read_permission_remains_unknown_not_verified(self):
        with patch("sora_commander_next.offline_broker_install_acl_audit._read_acl",
                   side_effect=PermissionError("read denied")):
            rows = audit_foundation_readonly()
        self.assertEqual([r.state for r in rows],
                         ["ACCESS_OR_PATH_UNAVAILABLE"] * 3)
        self.assertFalse(any(r.checked or r.protected_release_verified
                             or r.launch_authorized for r in rows))

    @unittest.skipUnless(os.name == "nt", "Windows Known Folder path observation")
    def test_other_drive_known_folders_are_audited_without_c_drive_fallback(self):
        observed = {
            "program_files": r"E:\Apps\Program Files",
            "program_data": r"F:\System\ProgramData",
            "local_app_data": r"G:\Users\別ユーザー\AppData\Local",
            "architecture": "AMD64",
        }
        with patch("sora_commander_next.offline_broker_install_acl_audit.observe_machine_known_folders_readonly",
                   return_value=observed), patch(
                       "sora_commander_next.offline_broker_install_acl_audit._read_acl",
                       side_effect=PermissionError("read denied")) as reader:
            rows = audit_foundation_readonly()
        self.assertEqual([str(call.args[0]).casefold()
                          for call in reader.call_args_list], [
            r"e:\apps\program files\soracommandernext",
            r"f:\system\programdata\soracommandernext",
            r"f:\system\programdata\soracommandernext\trust",
        ])
        self.assertTrue(all(not row.checked and not row.launch_authorized
                            and not row.protected_release_verified for row in rows))

    @unittest.skipUnless(os.name == "nt", "Windows Known Folder path observation")
    def test_missing_or_unsafe_os_folder_observation_never_reads_acls(self):
        good = {
            "program_files": r"E:\Program Files",
            "program_data": r"F:\ProgramData",
        }
        invalid = (
            None,
            {},
            {**good, "program_files": r"\\server\share\Program Files"},
            {**good, "program_files": r"E:\Program Files\..\Windows"},
            {**good, "program_data": r"E:\Program Files\Data"},
            {**good, "program_files": None},
        )
        for observed in invalid:
            with self.subTest(observed=observed), patch(
                    "sora_commander_next.offline_broker_install_acl_audit.observe_machine_known_folders_readonly",
                    return_value=observed), patch(
                    "sora_commander_next.offline_broker_install_acl_audit._read_acl") as reader:
                rows = audit_foundation_readonly()
                reader.assert_not_called()
                self.assertEqual(tuple(row.state for row in rows),
                                 ("ACCESS_OR_PATH_UNAVAILABLE",) * 3)
                self.assertFalse(any(row.checked or row.launch_authorized
                                     for row in rows))

    @unittest.skipUnless(os.name == "nt", "Windows Known Folder path observation")
    def test_os_known_folder_query_denied_is_unknown_not_default(self):
        with patch(
                "sora_commander_next.offline_broker_install_acl_audit.observe_machine_known_folders_readonly",
                side_effect=PermissionError("no observation")), patch(
                "sora_commander_next.offline_broker_install_acl_audit._read_acl") as reader:
            rows = audit_foundation_readonly()
        reader.assert_not_called()
        self.assertTrue(all(row.state == "ACCESS_OR_PATH_UNAVAILABLE"
                            and not row.checked for row in rows))

    @unittest.skipUnless(os.name == "nt", "Windows installation foundation")
    def test_real_windows_foundation_readonly_observation(self):
        rows = audit_foundation_readonly()
        self.assertEqual(tuple(row.label for row in rows),
                         ("CODE_ROOT", "ADMIN_DATA", "TRUST"))
        self.assertEqual(rows[0].state, "DIRECTORY_DACL_MATCH_ONLY")
        self.assertTrue(rows[0].checked)
        self.assertTrue(rows[0].code_root_acl_match)
        for row in rows:
            with self.subTest(label=row.label):
                self.assertIs(type(row), DirectoryAclObservation)
                self.assertFalse(row.protected_release_verified)
                self.assertFalse(row.launch_authorized)
                self.assertIn(row.state, (
                    "DIRECTORY_DACL_MATCH_ONLY",
                    "ACL_OBSERVED_NOT_ACCEPTED",
                    "ACCESS_OR_PATH_UNAVAILABLE"))
        # Protected data ACLs may not be readable to a non-elevated user.
        # This is an UNKNOWN review, never a denial bypass or auto elevation.
        self.assertFalse(rows[1].code_root_acl_match)
        self.assertFalse(rows[2].code_root_acl_match)


if __name__ == "__main__":
    unittest.main()
