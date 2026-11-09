"""Mocked listener handoff; never creates a real pipe."""
import os
import unittest
from unittest.mock import patch, PropertyMock
from ctypes import wintypes

@unittest.skipUnless(os.name == "nt", "Windows only")
class ListenerHandoffTests(unittest.TestCase):
    def test_timeout_finally_retains_pipe_until_completion(self):
        import sora_commander_next.windows_pipe_listener as mod
        from sora_commander_next.windows_owner import OwnerSessionGuard
        from sora_commander_next.windows_pipe_lifetime import PipeIoRegistry
        registry = PipeIoRegistry()
        guard = object.__new__(OwnerSessionGuard)
        guard.verify = lambda: True
        def timeout(handle, **kwargs):
            operation = registry.begin(handle, 4)
            operation.terminal = False
            operation.probe = lambda: setattr(operation, "terminal", True)
            registry.finish(operation)
            raise TimeoutError("mock pending connection")
        with patch.object(mod, "REGISTRY", registry), \
             patch.object(OwnerSessionGuard, "sid", new_callable=PropertyMock, return_value="S-1-5-21-1234"), \
             patch.object(mod, "ConvertSDDL", return_value=True), \
             patch.object(mod, "CreateNamedPipeW", return_value=123), \
             patch.object(mod, "connect_until", side_effect=timeout), \
             patch.object(mod, "WindowsPipeRequestContext") as context, \
             patch.object(mod, "DisconnectNamedPipe", return_value=True) as disconnect, \
             patch.object(mod, "CloseHandle", return_value=True) as close, \
             patch.object(mod, "LocalFree", return_value=None):
            with self.assertRaises(TimeoutError):
                mod.serve_once(owner_guard=guard, dispatcher=object(),
                               pipe_name=r"\\.\pipe\SoraCommanderNext-test")
            context.assert_not_called()
            close.assert_not_called()
            disconnect.assert_not_called()
            self.assertEqual(registry.snapshot()["connections"], 1)
            registry.reap_once()
            close.assert_called_once_with(123)
            disconnect.assert_called_once_with(123)

    def test_handle_wrapped_and_first_instance(self):
        import sora_commander_next.windows_pipe_listener as mod
        from sora_commander_next.windows_owner import OwnerSessionGuard
        guard = object.__new__(OwnerSessionGuard)
        guard._identity = object()
        guard.verify = lambda: True
        events = []
        payload = b'{"version":1,"op":"health"}'
        def pipe_create(*args):
            self.assertTrue(args[1] & mod.FILE_FLAG_FIRST_PIPE_INSTANCE)
            events.append("create")
            return 123
        class Context:
            def __init__(self, **kwargs):
                pass
            def handle(inner, handle, raw):
                self.assertIsInstance(handle, wintypes.HANDLE)
                self.assertEqual(handle.value, 123)
                self.assertEqual(raw, payload)
                events.append("dispatch")
                return b'{"version":1,"status":"OK","request_id":null}'
        with patch.object(OwnerSessionGuard, "sid", new_callable=PropertyMock, return_value="S-1-5-21-1234"), \
             patch.object(mod, "ConvertSDDL", return_value=True), \
             patch.object(mod, "CreateNamedPipeW", side_effect=pipe_create), \
             patch.object(mod, "connect_until", return_value=True), \
             patch.object(mod, "read_exact_until", side_effect=[len(payload).to_bytes(4, "little"), payload]), \
             patch.object(mod, "WindowsPipeRequestContext", Context), \
             patch.object(mod, "write_all_until", side_effect=lambda h, data, deadline: events.append("write") or len(data)), \
             patch.object(mod, "DisconnectNamedPipe", return_value=True), \
             patch.object(mod, "CloseHandle", return_value=True), \
             patch.object(mod, "LocalFree", return_value=None):
            mod.serve_once(owner_guard=guard, dispatcher=object(),
                           pipe_name=r"\\.\pipe\SoraCommanderNext-test")
        self.assertEqual(events, ["create", "dispatch", "write"])

if __name__ == "__main__":
    unittest.main()
