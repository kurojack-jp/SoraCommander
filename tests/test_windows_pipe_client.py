"""Safe negative-only tests: no pipe, no impersonation, no process launch."""
import os
import unittest

@unittest.skipUnless(os.name == "nt", "Windows only")
class WindowsPipeClientTests(unittest.TestCase):
    def test_missing_impersonation_token_denied(self):
        from sora_commander_next.windows_owner import current_windows_owner
        from sora_commander_next.windows_pipe_client import verify_impersonated_client
        owner = current_windows_owner()
        with self.assertRaises(PermissionError):
            verify_impersonated_client(owner)

    def test_no_caller_supplied_identity(self):
        from sora_commander_next.windows_pipe_client import verify_impersonated_client
        with self.assertRaises(PermissionError):
            verify_impersonated_client({"sid": "owner", "session": 1})

    def test_unverified_owner_denied(self):
        from sora_commander_next.windows_owner import current_windows_owner
        from dataclasses import replace
        from sora_commander_next.windows_pipe_client import verify_impersonated_client
        owner = current_windows_owner()
        with self.assertRaises(PermissionError):
            verify_impersonated_client(replace(owner, active=False))


if __name__ == "__main__":
    unittest.main()
