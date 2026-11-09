import os
import unittest
from unittest.mock import patch
from ctypes import wintypes

@unittest.skipUnless(os.name == "nt", "Windows only")
class PipeContextTests(unittest.TestCase):
    def setUp(self):
        from sora_commander_next.windows_owner import OwnerSessionGuard
        self.guard = object.__new__(OwnerSessionGuard)
        self.guard._identity = object()
        self.checks = []
        self.guard.verify = lambda: self.checks.append("owner") or True
        self.calls = []
        class Dispatcher:
            def handle(inner, raw):
                self.calls.append(raw)
                return b'{"version":1,"status":"OK","request_id":null}'
        from sora_commander_next.windows_pipe_context import WindowsPipeRequestContext
        self.context = WindowsPipeRequestContext(owner_guard=self.guard, dispatcher=Dispatcher())

    def test_missing_handle_denied(self):
        from sora_commander_next.windows_pipe_context import PipeImpersonationError
        with self.assertRaises(PipeImpersonationError):
            self.context.handle(wintypes.HANDLE(), b'{"version":1,"op":"health"}')
        self.assertEqual(self.calls, [])

    def test_revert_before_dispatch(self):
        import sora_commander_next.windows_pipe_context as mod
        events = []
        def dispatch(raw):
            events.append("dispatch")
            return b'{"version":1,"status":"OK","request_id":null}'
        self.context._dispatcher.handle = dispatch
        with patch.object(mod, "_impersonate", side_effect=lambda _: events.append("impersonate") or True), \
             patch.object(mod, "verify_impersonated_client", side_effect=lambda _: events.append("verify") or True), \
             patch.object(mod, "_revert", side_effect=lambda: events.append("revert") or True):
            self.context.handle(wintypes.HANDLE(123), b'{"version":1,"op":"health"}')
        self.assertEqual(events, ["impersonate", "verify", "revert", "dispatch"])
        self.assertEqual(len(self.checks), 2)

    def test_verifier_error_always_reverts(self):
        import sora_commander_next.windows_pipe_context as mod
        events = []
        with patch.object(mod, "_impersonate", return_value=True), \
             patch.object(mod, "verify_impersonated_client", side_effect=PermissionError("denied")), \
             patch.object(mod, "_revert", side_effect=lambda: events.append("revert") or True):
            with self.assertRaises(PermissionError):
                self.context.handle(wintypes.HANDLE(123), b'{}')
        self.assertEqual(events, ["revert"])
        self.assertEqual(self.calls, [])

    def test_revert_failure_denies_dispatch(self):
        import sora_commander_next.windows_pipe_context as mod
        class FatalExit(BaseException):
            pass
        with patch.object(mod, "_impersonate", return_value=True), \
             patch.object(mod, "verify_impersonated_client", return_value=True), \
             patch.object(mod, "_revert", return_value=False), \
             patch.object(mod.os, "_exit", side_effect=FatalExit) as fatal:
            with self.assertRaises(FatalExit):
                self.context.handle(wintypes.HANDLE(123), b'{}')
        fatal.assert_called_once_with(71)
        self.assertEqual(self.calls, [])

    def test_impersonate_failure_never_verifies_or_dispatches(self):
        import sora_commander_next.windows_pipe_context as mod
        with patch.object(mod, "_impersonate", return_value=False), \
             patch.object(mod, "verify_impersonated_client") as verify:
            with self.assertRaises(PermissionError):
                self.context.handle(wintypes.HANDLE(123), b'{}')
            verify.assert_not_called()
        self.assertEqual(self.calls, [])

if __name__ == "__main__":
    unittest.main()
