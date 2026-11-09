"""Pure ownership and mocked native races; no real pipe or HANDLE operations."""
import ctypes
import gc
import os
import unittest
import weakref
from unittest.mock import Mock, patch

from sora_commander_next.windows_pipe_lifetime import PipeIoRegistry, PipeLifetimeError


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = PipeIoRegistry()
        self.events = []
        self.connection = self.registry.reserve_connection()
        self.connection.bind(123, disconnect=lambda h: self.events.append("disconnect"),
                             close=lambda h: self.events.append("close") or True)

    def test_pending_memory_survives_caller_gc_until_late_completion(self):
        operation = self.registry.begin(123, 4)
        operation.buffer = ctypes.create_string_buffer(4)
        reference = weakref.ref(operation.buffer)
        operation.terminal = False
        operation.probe = lambda: setattr(operation, "terminal", True)
        self.registry.finish(operation)
        self.connection.close()
        gc.collect()
        self.assertIsNotNone(reference())
        self.assertEqual(self.events, [])
        result = self.registry.reap_once()
        self.assertEqual(result, {"connections": 0, "operations": 0,
                                  "buffer_bytes": 0, "halted": True})
        self.assertEqual(self.events, ["disconnect", "close"])
        # Successful cleanup does not silently restore admission/old consent.
        with self.assertRaises(PipeLifetimeError):
            self.registry.reserve_connection()

    def test_native_gate_prevents_reaping_and_pipe_close(self):
        operation = self.registry.begin(123, 4)
        operation.probe = Mock()
        operation.terminal = False
        self.connection.close()
        self.registry.reap_once()
        operation.probe.assert_not_called()
        self.assertEqual(self.events, [])
        operation.terminal = True
        self.registry.finish(operation)
        self.assertEqual(self.events, ["disconnect", "close"])

    def test_one_operation_per_connection_and_admission_caps(self):
        operation = self.registry.begin(123, 4)
        with self.assertRaises(PipeLifetimeError):
            self.registry.begin(123, 1)
        for _ in range(3):
            self.registry.reserve_connection()
        with self.assertRaises(PipeLifetimeError):
            self.registry.reserve_connection()
        self.registry.finish(operation)
        self.assertEqual(self.registry.snapshot()["buffer_bytes"], 0)

    def test_byte_budget_is_reserved_before_allocation(self):
        registry = PipeIoRegistry(max_bytes=4)
        connection = registry.reserve_connection()
        connection.bind(123, disconnect=lambda h: True, close=lambda h: True)
        with self.assertRaises(PipeLifetimeError):
            registry.begin(123, 5)
        self.assertEqual(registry.snapshot()["operations"], 0)
        connection.close()

    def test_unknown_event_close_is_not_retried_or_refunded(self):
        operation = self.registry.begin(123, 4)
        operation.event = 42
        operation.close_event = Mock(return_value=False)
        with self.assertRaises(PipeLifetimeError):
            self.registry.finish(operation)
        self.connection.close()
        self.registry.reap_once()
        operation.close_event.assert_called_once_with(42)
        self.assertEqual(self.events, [])
        self.assertEqual(self.registry.snapshot()["buffer_bytes"], 4)

    def test_unknown_pipe_close_is_not_retried(self):
        self.connection.close_handle = Mock(return_value=False)
        with self.assertRaises(PipeLifetimeError):
            self.connection.close()
        self.connection.close()
        self.connection.close_handle.assert_called_once_with(123)
        self.assertTrue(self.registry.snapshot()["halted"])
        self.assertEqual(self.registry.snapshot()["connections"], 1)


@unittest.skipUnless(os.name == "nt", "Windows ctypes definitions required")
class NativeContractTests(unittest.TestCase):
    def setUp(self):
        from sora_commander_next import windows_pipe_connect as api
        self.api = api
        self.registry = PipeIoRegistry()
        self.connection = self.registry.reserve_connection()
        self.pipe_close = Mock(return_value=True)
        self.connection.bind(123, disconnect=lambda h: True, close=self.pipe_close)
        self.clock = lambda: 1
        self.issue = Mock(return_value=False)
        self.wait = Mock(return_value=258)
        self.cancel = Mock(return_value=False)
        self.result = Mock(return_value=False)
        self.close = Mock(return_value=True)
        self.error = Mock(side_effect=[997, 996])

    def run_io(self):
        return self.api._run_operation(
            123, size=4, make_buffer=lambda: ctypes.create_string_buffer(b"data", 4),
            issue=self.issue, is_connect=False, deadline=5, clock=self.clock,
            registry=self.registry,
            api=(lambda *a: 42, self.wait, self.cancel, self.result, self.close, self.error),
            error_type=OSError)

    def test_cancel_false_and_incomplete_never_release_then_late_success_only_reaps(self):
        with self.assertRaises(TimeoutError):
            self.run_io()
        self.connection.close()
        self.cancel.assert_called_once()
        self.close.assert_not_called()
        self.pipe_close.assert_not_called()
        self.assertIs(self.result.call_args.args[-1], False)
        operation = next(iter(self.registry._operations))
        self.assertTrue(operation.expired)
        self.assertEqual(operation.buffer.raw, b"data")
        self.result.return_value = True
        self.registry.reap_once()
        self.close.assert_called_once_with(42)
        self.pipe_close.assert_called_once_with(123)
        self.assertTrue(operation.expired)

    def test_cancel_success_and_invalid_handle_are_not_completion(self):
        self.cancel.return_value = True
        self.error.side_effect = [997, 6]
        with self.assertRaises(TimeoutError):
            self.run_io()
        self.close.assert_not_called()
        self.assertTrue(self.registry.snapshot()["halted"])

    def test_native_issue_exception_retains_every_reference(self):
        self.issue.side_effect = RuntimeError("native boundary uncertainty")
        with self.assertRaises(RuntimeError):
            self.run_io()
        self.close.assert_not_called()
        self.assertEqual(self.registry.snapshot()["operations"], 1)

    def test_expired_success_does_not_return_payload(self):
        self.issue.return_value = True
        self.result.return_value = True
        self.clock = Mock(side_effect=[1, 6])
        with self.assertRaises(TimeoutError):
            self.run_io()
        self.close.assert_called_once_with(42)
        self.assertEqual(self.registry.snapshot()["operations"], 0)

    def test_cancel_exception_retains_pending_memory(self):
        self.cancel.side_effect = OSError("native cancel failed")
        with self.assertRaises(OSError):
            self.run_io()
        self.close.assert_not_called()
        self.assertEqual(self.registry.snapshot()["buffer_bytes"], 4)

    def test_explicit_issue_failure_needs_no_completion_query(self):
        self.error.side_effect = [5]
        with self.assertRaises(OSError):
            self.run_io()
        self.result.assert_not_called()
        self.close.assert_called_once_with(42)
        self.assertEqual(self.registry.snapshot()["operations"], 0)

    def test_event_allocation_failure_returns_reserved_capacity(self):
        with self.assertRaises(OSError):
            self.api._run_operation(
                123, size=4, make_buffer=lambda: ctypes.create_string_buffer(4),
                issue=self.issue, is_connect=False, deadline=5, clock=self.clock,
                registry=self.registry,
                api=(lambda *a: 0, self.wait, self.cancel, self.result, self.close, self.error),
                error_type=OSError)
        self.issue.assert_not_called()
        self.close.assert_not_called()
        self.assertEqual(self.registry.snapshot()["buffer_bytes"], 0)
