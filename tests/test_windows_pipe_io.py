import ctypes
import os
import unittest
from unittest.mock import patch

@unittest.skipUnless(os.name == "nt", "Windows only")
class PipeReadTests(unittest.TestCase):
    def test_timeout_when_no_bytes(self):
        from sora_commander_next import windows_pipe_io as io
        def peek(handle, buffer, length, read, available, left):
            ctypes.cast(available, ctypes.POINTER(ctypes.c_ulong))[0] = 0
            return True
        with patch.object(io, "_peek", side_effect=peek):
            with self.assertRaises(io.DeadlineExceeded):
                io.read_exact_until(123, 4, 1, clock=lambda: 2)

    def test_partial_arrivals(self):
        from sora_commander_next import windows_pipe_io as io
        sizes = iter([0, 2, 2])
        chunks = iter([b"ab", b"cd"])
        def peek(handle, buffer, length, read, available, left):
            ctypes.cast(available, ctypes.POINTER(ctypes.c_ulong))[0] = next(sizes)
            return True
        def read(handle, buf, size, count, overlapped):
            data = next(chunks)
            ctypes.memmove(buf, data, len(data))
            ctypes.cast(count, ctypes.POINTER(ctypes.c_ulong))[0] = len(data)
            return True
        with patch.object(io, "_peek", side_effect=peek), patch.object(io, "_read", side_effect=read):
            self.assertEqual(io.read_exact_until(123, 4, 100000000000,
                             pause=lambda _: None), b"abcd")

    def test_disconnected(self):
        from sora_commander_next import windows_pipe_io as io
        with patch.object(io, "_peek", return_value=False):
            with self.assertRaises(OSError):
                io.read_exact_until(123, 4, 100000000000)

    def test_rejects_oversized_read(self):
        from sora_commander_next import windows_pipe_io as io
        with self.assertRaises(ValueError):
            io.read_exact_until(123, 8193, 100000000000)
