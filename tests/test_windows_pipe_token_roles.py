"""Mock token representations only: no OS identity lookup or impersonation."""
import ctypes
from ctypes import wintypes
from dataclasses import replace
import os
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name == "nt", "Windows ctypes definitions required")
class TokenRoleTests(unittest.TestCase):
    def setUp(self):
        from sora_commander_next import windows_pipe_client as module
        self.module = module
        self.expected = module.WindowsOwnerIdentity("S-1-5-21-1-2-3-1001", 2, (9, 0), True)
        self.stats = module.TokenStatistics()
        self.stats.TokenType = 2
        self.stats.ImpersonationLevel = 1
        self.stats.AuthenticationId.LowPart = 9
        self.sid = ctypes.create_unicode_buffer(self.expected.sid)
        self.user = ctypes.create_string_buffer(16)
        ctypes.cast(self.user, ctypes.POINTER(ctypes.c_void_p))[0] = 1
        self.session = wintypes.DWORD(2)

        def open_token(thread, rights, as_self, target):
            self.assertEqual(rights, 8)
            self.assertIs(as_self, True)
            ctypes.cast(target, ctypes.POINTER(wintypes.HANDLE))[0] = 77
            return True

        def convert_sid(sid, target):
            ctypes.cast(target, ctypes.POINTER(wintypes.LPWSTR))[0] = ctypes.cast(self.sid, wintypes.LPWSTR)
            return True

        def token_buffer(handle, kind):
            value = {1: self.user, 10: self.stats, 12: self.session}[kind]
            size = ctypes.sizeof(value)
            return ctypes.create_string_buffer(ctypes.string_at(ctypes.byref(value), size), size)

        for name, kwargs in (
                ("GetCurrentThread", {"return_value": -2}),
                ("OpenThreadToken", {"side_effect": open_token}),
                ("token_bytes", {"side_effect": token_buffer}),
                ("ConvertSidToStringSidW", {"side_effect": convert_sid}),
                ("LocalFree", {"return_value": None}),
                ("CloseHandle", {"return_value": True})):
            patcher = patch.object(module, name, **kwargs)
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            if name == "CloseHandle":
                self.close = mock

    def test_ledger_identification_is_separate_from_owner_impersonation(self):
        self.assertTrue(self.module.verify_identified_ledger_client(self.expected))
        with self.assertRaises(PermissionError):
            self.module.verify_impersonated_client(self.expected)
        self.assertEqual(self.close.call_count, 2)

    def test_each_endpoint_rejects_other_levels(self):
        for level in (0, 1, 2, 3, 4):
            self.stats.ImpersonationLevel = level
            for verifier, accepted in ((self.module.verify_identified_ledger_client, (1,)),
                                       (self.module.verify_impersonated_client, (2, 3))):
                with self.subTest(level=level, verifier=verifier.__name__):
                    if level in accepted:
                        self.assertTrue(verifier(self.expected))
                    else:
                        with self.assertRaises(PermissionError):
                            verifier(self.expected)

    def test_primary_token_is_denied_even_with_matching_level(self):
        self.stats.TokenType = 1
        with self.assertRaises(PermissionError):
            self.module.verify_identified_ledger_client(self.expected)
        self.close.assert_called_once()

    def test_sid_luid_and_session_are_all_bound(self):
        for identity in (replace(self.expected, sid="S-1-5-21-1-2-3-1002"),
                         replace(self.expected, logon_luid=(10, 0)),
                         replace(self.expected, session_id=3)):
            with self.subTest(identity=identity), self.assertRaises(PermissionError):
                self.module.verify_identified_ledger_client(identity)

    def test_inactive_and_payload_identity_are_rejected_before_token_open(self):
        for identity in (replace(self.expected, active=False), replace(self.expected, active=1),
                         {"sid": self.expected.sid}, None):
            with self.subTest(identity=identity), self.assertRaises(PermissionError):
                self.module.verify_identified_ledger_client(identity)
        self.close.assert_not_called()

    def test_truncated_token_structures_are_denied_and_handle_closed(self):
        for name in ("user", "stats", "session"):
            original = getattr(self, name)
            setattr(self, name, ctypes.create_string_buffer(1))
            with self.subTest(name=name), self.assertRaises(PermissionError):
                self.module.verify_identified_ledger_client(self.expected)
            setattr(self, name, original)
        self.assertEqual(self.close.call_count, 3)

    def test_token_query_failure_closes_handle(self):
        with patch.object(self.module, "token_bytes", side_effect=OSError("query refused")):
            with self.assertRaises(OSError):
                self.module.verify_identified_ledger_client(self.expected)
        self.close.assert_called_once()
