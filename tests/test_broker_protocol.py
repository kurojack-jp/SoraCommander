import unittest
from sora_commander_next.broker_protocol import decode_request, encode_response, ProtocolRejected


class ProtocolTests(unittest.TestCase):
    def test_health(self):
        self.assertEqual(decode_request(b'{"version":1,"op":"health"}')["op"], "health")

    def test_query(self):
        self.assertEqual(decode_request(b'{"version":1,"op":"query","request_id":"' + b"a"*32 + b'"}')["op"], "query")

    def test_cancel(self):
        self.assertEqual(decode_request(b'{"version":1,"op":"cancel","request_id":"' + b"a"*32 + b'"}')["op"], "cancel")

    def test_duplicate(self):
        with self.assertRaises(ProtocolRejected):
            decode_request(b'{"version":1,"version":1,"op":"health"}')

    def test_extra(self):
        with self.assertRaises(ProtocolRejected):
            decode_request(b'{"version":1,"op":"health","command":"whoami"}')

    def test_no_launch(self):
        with self.assertRaises(ProtocolRejected):
            decode_request(b'{"version":1,"op":"submit"}')

    def test_oversize(self):
        with self.assertRaises(ProtocolRejected):
            decode_request(b"x"*8193)

    def test_bad_utf8(self):
        with self.assertRaises(ProtocolRejected):
            decode_request(b"\xff")

    def test_nonfinite(self):
        with self.assertRaises(ProtocolRejected):
            decode_request(b'{"version":NaN,"op":"health"}')

    def test_bad_id(self):
        with self.assertRaises(ProtocolRejected):
            decode_request(b'{"version":1,"op":"query","request_id":"../"}')

    def test_response(self):
        self.assertIn(b'"status":"UNKNOWN"', encode_response(
            {"version":1,"status":"UNKNOWN","request_id":None}))


if __name__ == "__main__":
    unittest.main()
