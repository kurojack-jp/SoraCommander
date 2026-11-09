import unittest
from sora_commander_next.broker_deadline import wait_for_bytes, DeadlineExceeded


class DeadlineTests(unittest.TestCase):
    def test_immediate_ready(self):
        self.assertEqual(wait_for_bytes(lambda: 5, minimum=4), 5)

    def test_ready_after_poll(self):
        values = iter([0, 1, 4])
        self.assertEqual(wait_for_bytes(lambda: next(values), minimum=4,
                                        pause=lambda _: None), 4)

    def test_timeout(self):
        values = iter([0, 0, 0])
        times = iter([0, 0, 0.5, 1.1])
        with self.assertRaises(DeadlineExceeded):
            wait_for_bytes(lambda: next(values), minimum=4,
                           timeout_seconds=1, clock=lambda: next(times),
                           pause=lambda _: None)

    def test_bad_availability_denied(self):
        with self.assertRaises(ValueError):
            wait_for_bytes(lambda: True, minimum=1)

    def test_invalid_minimum(self):
        with self.assertRaises(ValueError):
            wait_for_bytes(lambda: 1, minimum=0)

    def test_invalid_timeout(self):
        with self.assertRaises(ValueError):
            wait_for_bytes(lambda: 1, minimum=1, timeout_seconds=61)


if __name__ == "__main__":
    unittest.main()
