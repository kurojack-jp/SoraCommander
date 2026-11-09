import unittest
from counter import add, subtract

class CounterTests(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(add(2, 3), 5)
    def test_negative(self):
        self.assertEqual(add(-4, 1), -3)
    def test_zero(self):
        self.assertEqual(add(0, 0), 0)

    def test_subtract_positive(self):
        self.assertEqual(subtract(5, 3), 2)

    def test_subtract_negative(self):
        self.assertEqual(subtract(-4, -1), -3)

if __name__ == '__main__':
    unittest.main()
