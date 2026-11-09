import os
import unittest
from unittest.mock import patch

@unittest.skipUnless(os.name == "nt", "Windows only")
class ConnectTests(unittest.TestCase):
    def setUp(self):
        from sora_commander_next.windows_pipe_lifetime import PipeIoRegistry
        self.registry = PipeIoRegistry()
        self.connection = self.registry.reserve_connection()
        self.connection.bind(123, disconnect=lambda h: True, close=lambda h: True)
        patcher = patch("sora_commander_next.windows_pipe_connect.REGISTRY", self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.connection.close)

    def test_immediate_connected(self):
        import sora_commander_next.windows_pipe_connect as m
        with patch.object(m, "_create_event", return_value=42), \
             patch.object(m, "_connect", return_value=True), \
             patch.object(m, "_close") as close:
            self.assertTrue(m.connect_until(123))
            close.assert_called_once_with(42)

    def test_rejects_bad_timeout(self):
        import sora_commander_next.windows_pipe_connect as m
        with self.assertRaises(ValueError):
            m.connect_until(123, timeout_ms=0)

    def test_event_failure(self):
        import sora_commander_next.windows_pipe_connect as m
        with patch.object(m, "_create_event", return_value=0):
            with self.assertRaises(m.PipeConnectError):
                m.connect_until(123)

    def test_pending_timeout_cancels_and_drains(self):
        import ctypes
        import sora_commander_next.windows_pipe_connect as m
        with patch.object(m, "_create_event", return_value=42), \
             patch.object(m, "_connect", return_value=False), \
             patch.object(m.ctypes, "get_last_error", side_effect=[m.ERROR_IO_PENDING, 995]), \
             patch.object(m, "_wait", return_value=m.WAIT_TIMEOUT), \
             patch.object(m, "_cancel", return_value=True) as cancel, \
             patch.object(m, "_result", return_value=False) as drain, \
             patch.object(m, "_close") as close:
            with self.assertRaises(TimeoutError):
                m.connect_until(123, timeout_ms=5)
            cancel.assert_called_once()
            drain.assert_called_once()
            self.assertIs(drain.call_args.args[-1], False)
            close.assert_called_once_with(42)

    def test_pending_cancel_not_found_retains_memory_and_event(self):
        import sora_commander_next.windows_pipe_connect as m
        with patch.object(m, "_create_event", return_value=42), \
             patch.object(m, "_connect", return_value=False), \
             patch.object(m.ctypes, "get_last_error", side_effect=[997, 996]), \
             patch.object(m, "_wait", return_value=m.WAIT_TIMEOUT), \
             patch.object(m, "_cancel", return_value=False), \
             patch.object(m, "_result", return_value=False) as probe, \
             patch.object(m, "_close") as close:
            with self.assertRaises(TimeoutError):
                m.connect_until(123, timeout_ms=5)
            self.connection.close()
            self.assertEqual(self.registry.snapshot()["operations"], 1)
            self.assertEqual(self.registry.snapshot()["connections"], 1)
            self.assertTrue(self.registry.snapshot()["halted"])
            self.assertIs(probe.call_args.args[-1], False)
            close.assert_not_called()

    def test_pending_success(self):
        import sora_commander_next.windows_pipe_connect as m
        with patch.object(m, "_create_event", return_value=42), \
             patch.object(m, "_connect", return_value=False), \
             patch.object(m.ctypes, "get_last_error", return_value=m.ERROR_IO_PENDING), \
             patch.object(m, "_wait", return_value=m.WAIT_OBJECT_0), \
             patch.object(m, "_result", return_value=True), \
             patch.object(m, "_close"):
            self.assertTrue(m.connect_until(123, timeout_ms=5))
