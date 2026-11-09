import unittest

from arithmetic_report import describe


class ArithmeticReportTests(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(describe(5, 3), {"sum": 8, "difference": 2})

    def test_negative(self):
        self.assertEqual(describe(-4, -1), {"sum": -5, "difference": -3})

    def test_zero(self):
        self.assertEqual(describe(0, 0), {"sum": 0, "difference": 0})


if __name__ == "__main__":
    unittest.main()
