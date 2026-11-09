import io
import struct
import unittest
from sora_commander_next.broker_frames import (
    FrameRejected, pack_frame, read_frame, write_frame,
)
from sora_commander_next.broker_protocol import MAX_FRAME


class FrameTests(unittest.TestCase):
    def test_roundtrip(self):
        payload = b'{"version":1,"op":"health"}'
        stream = io.BytesIO(pack_frame(payload))
        self.assertEqual(read_frame(stream.read), payload)

    def test_maximum_payload(self):
        stream = io.BytesIO(pack_frame(b"x" * MAX_FRAME))
        self.assertEqual(len(read_frame(stream.read)), MAX_FRAME)

    def test_empty_denied(self):
        with self.assertRaises(FrameRejected):
            pack_frame(b"")

    def test_oversize_denied(self):
        with self.assertRaises(FrameRejected):
            pack_frame(b"x" * (MAX_FRAME + 1))

    def test_declared_oversize_does_not_read_body(self):
        calls = []
        def read(n):
            calls.append(n)
            return struct.pack("<I", MAX_FRAME + 1)
        with self.assertRaises(FrameRejected):
            read_frame(read)
        self.assertEqual(calls, [4])

    def test_zero_length_denied(self):
        with self.assertRaises(FrameRejected):
            read_frame(io.BytesIO(b"\x00\x00\x00\x00").read)

    def test_truncated_header_denied(self):
        with self.assertRaises(FrameRejected):
            read_frame(io.BytesIO(b"\x01\x00").read)

    def test_truncated_body_denied(self):
        with self.assertRaises(FrameRejected):
            read_frame(io.BytesIO(struct.pack("<I", 5) + b"abc").read)

    def test_write_all(self):
        stream = io.BytesIO()
        n = write_frame(stream.write, b"abc")
        self.assertEqual(n, 7)
        stream.seek(0)
        self.assertEqual(read_frame(stream.read), b"abc")

    def test_partial_write_denied(self):
        with self.assertRaises(FrameRejected):
            write_frame(lambda data: 2, b"abc")

    def test_bad_reader_return_denied(self):
        with self.assertRaises(FrameRejected):
            read_frame(lambda n: "no bytes")


if __name__ == "__main__":
    unittest.main()
