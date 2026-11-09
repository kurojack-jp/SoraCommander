import os
import unittest
from unittest.mock import patch

@unittest.skipUnless(os.name == "nt", "Windows only")
class TransferTests(unittest.TestCase):
    def setUp(self):
        from sora_commander_next.windows_pipe_lifetime import PipeIoRegistry
        self.registry = PipeIoRegistry()
        self.connection = self.registry.reserve_connection()
        self.connection.bind(123, disconnect=lambda h: True, close=lambda h: True)
        patcher = patch("sora_commander_next.windows_pipe_transfer.REGISTRY", self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.connection.close)

    def test_invalid_read(self):
        from sora_commander_next.windows_pipe_transfer import read_exact_until
        with self.assertRaises(ValueError):
            read_exact_until(123, 8193, 100000000000)

    def test_invalid_write(self):
        from sora_commander_next.windows_pipe_transfer import write_all_until
        with self.assertRaises(ValueError):
            write_all_until(123, b"", 100000000000)

    def test_deadline_before_io(self):
        from sora_commander_next.windows_pipe_transfer import read_exact_until
        with self.assertRaises(TimeoutError):
            read_exact_until(123, 4, 0, clock=lambda: 1)

    def test_event_creation_failure(self):
        import sora_commander_next.windows_pipe_transfer as m
        with patch.object(m, "_create_event", return_value=0):
            with self.assertRaises(m.PipeTransferError):
                m.read_exact_until(123, 4, 100, clock=lambda: 1)

    def test_partial_read(self):
        import ctypes
        import sora_commander_next.windows_pipe_transfer as m
        chunks = iter([b"ab", b"cd"])
        def operation(handle, buf, size, count, op):
            self.assertIsNone(count)
            chunk = next(chunks)
            ctypes.memmove(buf, chunk, len(chunk))
            return True
        def result(handle, op, count, wait):
            self.assertIs(wait, False)
            ctypes.cast(count, ctypes.POINTER(ctypes.c_ulong))[0] = 2
            return True
        with patch.object(m, "_create_event", return_value=42), \
             patch.object(m, "_read", side_effect=operation), \
             patch.object(m, "_result", side_effect=result), \
             patch.object(m, "_close"):
            self.assertEqual(m.read_exact_until(123, 4, 100, clock=lambda: 1), b"abcd")

    def test_partial_write(self):
        import ctypes
        import sora_commander_next.windows_pipe_transfer as m
        writes = []
        def operation(handle, buf, size, count, op):
            self.assertIsNone(count)
            writes.append(ctypes.string_at(buf, size))
            return True
        def result(handle, op, count, wait):
            self.assertIs(wait, False)
            ctypes.cast(count, ctypes.POINTER(ctypes.c_ulong))[0] = 2
            return True
        with patch.object(m, "_create_event", return_value=42), \
             patch.object(m, "_write", side_effect=operation), \
             patch.object(m, "_result", side_effect=result), \
             patch.object(m, "_close"):
            self.assertEqual(m.write_all_until(123, b"abcd", 100, clock=lambda: 1), 4)
        self.assertEqual(writes, [b"abcd", b"cd"])

    def test_pending_write_timeout_cancels_and_drains(self):
        import sora_commander_next.windows_pipe_transfer as m
        with patch.object(m, "_create_event", return_value=42), \
             patch.object(m, "_write", return_value=False), \
             patch.object(m.ctypes, "get_last_error", side_effect=[m.ERROR_IO_PENDING, 995]), \
             patch.object(m, "_wait", return_value=m.WAIT_TIMEOUT), \
             patch.object(m, "_cancel", return_value=True) as cancel, \
             patch.object(m, "_result", return_value=False) as drain, \
             patch.object(m, "_close") as close:
            with self.assertRaises(TimeoutError):
                m.write_all_until(123, b"data", 100, clock=lambda: 1)
            cancel.assert_called_once()
            drain.assert_called_once()
            self.assertIs(drain.call_args.args[-1], False)
            close.assert_called_once_with(42)

    def test_pending_read_success(self):
        import ctypes
        import sora_commander_next.windows_pipe_transfer as m
        def op(handle, buf, size, count, overlapped):
            ctypes.memmove(buf, b"ok", 2)
            return False
        def result(handle, overlapped, count, wait):
            ctypes.cast(count, ctypes.POINTER(ctypes.c_ulong))[0] = 2
            return True
        with patch.object(m, "_create_event", return_value=42), \
             patch.object(m, "_read", side_effect=op), \
             patch.object(m.ctypes, "get_last_error", return_value=m.ERROR_IO_PENDING), \
             patch.object(m, "_wait", return_value=m.WAIT_OBJECT_0), \
             patch.object(m, "_result", side_effect=result), \
             patch.object(m, "_close"):
            self.assertEqual(m.read_exact_until(123, 2, 100, clock=lambda: 1), b"ok")
