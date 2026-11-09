"""Literal byte expectations for the independent test fixture encoder."""
import copy
import unittest

from release_wire_fixture_helpers import canonical_json_wire


class CanonicalJsonWireTests(unittest.TestCase):
    def test_ascii_bytes(self):
        raw = canonical_json_wire({"text": "ASCII"})
        self.assertIsInstance(raw, bytes)
        self.assertTrue(raw.isascii())
        self.assertEqual(raw, b'{"text":"ASCII"}\n')

    def test_nonascii_is_escaped(self):
        raw = canonical_json_wire({"text": "\u00e9\u65e5\U0001f600"})
        self.assertTrue(raw.isascii())
        self.assertEqual(raw, b'{"text":"\\u00e9\\u65e5\\ud83d\\ude00"}\n')

    def test_nested_keys_are_sorted(self):
        value = {"z": {"b": 2, "a": 1}, "a": [{"d": 4, "c": 3}]}
        self.assertEqual(canonical_json_wire(value),
                         b'{"a":[{"c":3,"d":4}],"z":{"a":1,"b":2}}\n')

    def test_compact_separators(self):
        self.assertEqual(canonical_json_wire({"b": [1, 2], "a": True}),
                         b'{"a":true,"b":[1,2]}\n')

    def test_exactly_one_trailing_newline(self):
        raw = canonical_json_wire({"text": "line\n"})
        self.assertEqual(raw, b'{"text":"line\\n"}\n')
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)

    def test_input_is_unchanged(self):
        value = {"z": {"b": [2, 1], "a": "\u00e9"}, "a": [{"d": 4, "c": 3}]}
        before = copy.deepcopy(value)
        canonical_json_wire(value)
        self.assertEqual(value, before)
        self.assertEqual(list(value), ["z", "a"])
        self.assertEqual(list(value["z"]), ["b", "a"])
        self.assertEqual(list(value["a"][0]), ["d", "c"])


if __name__ == "__main__":
    unittest.main()
