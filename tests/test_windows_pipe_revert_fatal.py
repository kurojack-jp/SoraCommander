"""Fail-closed Win32 RevertToSelf scope, no real IPC or identity changes.

Mocked Windows impersonation functions in a DISPOSABLE child process
prove that failure to revert terminates the entire process, not merely
raising an exception that might leave the Broker thread impersonating.
No named pipe is opened, no Owner GUI, no command executes.
"""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from ctypes import wintypes

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.windows_pipe_context import (
    WindowsPipeRequestContext, PipeImpersonationError)
from sora_commander_next import windows_pipe_context as ipc


def fake_context():
    """Bypass trusted constructor ONLY in a test with mocked Owner guard."""
    context = WindowsPipeRequestContext.__new__(WindowsPipeRequestContext)
    dispatcher = SimpleNamespace(handle=lambda raw: b"readonly-mock-response")
    context._owner_guard = SimpleNamespace(
        verify=lambda: True, _identity=object())
    context._dispatcher = dispatcher
    context._gate_limits = {}
    return context


class RevertFailureTests(unittest.TestCase):
    def test_failure_to_revert_exits_entire_disposable_process(self):
        child_code = r"""
import sys
from ctypes import wintypes
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from sora_commander_next.windows_pipe_context import WindowsPipeRequestContext
from sora_commander_next import windows_pipe_context as ipc
ctx = WindowsPipeRequestContext.__new__(WindowsPipeRequestContext)
ctx._owner_guard = SimpleNamespace(verify=lambda: True, _identity=object())
ctx._dispatcher = SimpleNamespace(handle=lambda raw: b"should-never-dispatch")
with patch.object(ipc, "_impersonate", return_value=True), \
     patch.object(ipc, "verify_impersonated_client", return_value=True), \
     patch.object(ipc, "_revert", return_value=False):
    ctx.handle(wintypes.HANDLE(12345), b"non-transport-test-data")
sys.exit(42)  # Revert failure MUST exit before this marker.
"""
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", child_code,
             str(ROOT / "src")],
            timeout=10, capture_output=True, text=True)
        self.assertEqual(result.returncode, 71, result.stderr[-500:])
        self.assertNotIn("should-never-dispatch", result.stdout)

    def test_exception_from_revert_terminates_disposable_process(self):
        # An exception from the native call is also an UNKNOWN impersonation
        # state; no Python finally/outer handler is allowed to keep running.
        child_code = r"""
import sys
from ctypes import wintypes
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from sora_commander_next.windows_pipe_context import WindowsPipeRequestContext
from sora_commander_next import windows_pipe_context as ipc
ctx = WindowsPipeRequestContext.__new__(WindowsPipeRequestContext)
ctx._owner_guard = SimpleNamespace(verify=lambda: True, _identity=object())
ctx._dispatcher = SimpleNamespace(handle=lambda raw: b"never")
with patch.object(ipc, "_impersonate", return_value=True), \
     patch.object(ipc, "verify_impersonated_client", return_value=True), \
     patch.object(ipc, "_revert", side_effect=OSError("mock-revert-failure")):
    ctx.handle(wintypes.HANDLE(12345), b"mock")
sys.exit(42)
"""
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", child_code,
             str(ROOT / "src")],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 71, result.stderr[-500:])

    def test_valid_revert_happens_before_readonly_dispatch(self):
        ctx = fake_context()
        order = []
        def authenticated(_):
            order.append("verify_client")
            return True
        def revert():
            order.append("revert")
            return True
        def dispatch(raw):
            order.append("dispatch")
            return b"readonly-ok"
        ctx._dispatcher.handle = dispatch
        with patch.object(ipc, "_impersonate", return_value=True), \
             patch.object(ipc, "verify_impersonated_client", side_effect=authenticated), \
             patch.object(ipc, "_revert", side_effect=revert):
            response = ctx.handle(wintypes.HANDLE(12345), b"test-only")
        self.assertEqual(response, b"readonly-ok")
        self.assertEqual(order, ["verify_client", "revert", "dispatch"])

    def test_invalid_impersonated_identity_never_dispatches(self):
        ctx = fake_context()
        with patch.object(ipc, "_impersonate", return_value=True), \
             patch.object(ipc, "verify_impersonated_client", return_value=False), \
             patch.object(ipc, "_revert", return_value=True) as revert, \
             patch.object(ctx._dispatcher, "handle") as dispatch:
            with self.assertRaises(PipeImpersonationError):
                ctx.handle(wintypes.HANDLE(12345), b"test-only")
        revert.assert_called_once()
        dispatch.assert_not_called()

    def test_exception_during_identity_check_always_reverts(self):
        ctx = fake_context()
        with patch.object(ipc, "_impersonate", return_value=True), \
             patch.object(ipc, "verify_impersonated_client",
                          side_effect=PermissionError("fake-token")), \
             patch.object(ipc, "_revert", return_value=True) as revert, \
             patch.object(ctx._dispatcher, "handle") as dispatch:
            with self.assertRaises(PermissionError):
                ctx.handle(wintypes.HANDLE(12345), b"test-only")
        revert.assert_called_once()
        dispatch.assert_not_called()

    def test_failed_impersonation_never_calls_revert_or_dispatch(self):
        ctx = fake_context()
        with patch.object(ipc, "_impersonate", return_value=False), \
             patch.object(ipc, "_revert") as revert, \
             patch.object(ctx._dispatcher, "handle") as dispatch:
            with self.assertRaises(PipeImpersonationError):
                ctx.handle(wintypes.HANDLE(12345), b"test-only")
        revert.assert_not_called()
        dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
