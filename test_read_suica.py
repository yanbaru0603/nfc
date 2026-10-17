import unittest

from read_suica import decode_balance


class DecodeBalanceTest(unittest.TestCase):
    def test_little_endian_balance(self):
        record = bytes.fromhex("16010000354100000000881300000100")
        self.assertEqual(decode_balance(record), 5000)

    def test_zero_balance_with_existing_history(self):
        record = bytes.fromhex("16010000354100000000000000000100")
        self.assertEqual(decode_balance(record), 0)

    def test_empty_history(self):
        self.assertIsNone(decode_balance(bytes(16)))

    def test_invalid_record_size(self):
        for size in (0, 15, 17):
            with self.subTest(size=size), self.assertRaises(ValueError):
                decode_balance(bytes(size))


if __name__ == "__main__":
    unittest.main()
