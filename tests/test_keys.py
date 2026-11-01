import io
import unittest

from strain.keys import decode_keys, open_key_reader


class DecodeKeysTest(unittest.TestCase):
    def test_arrow_keys_and_letters(self) -> None:
        self.assertEqual(decode_keys("\033[Aj\033[Bk\tq"), ["up", "down", "down", "up", "tab", "q"])

    def test_application_mode_arrows_and_paging(self) -> None:
        self.assertEqual(decode_keys("\033OA\033OB\033[5~\033[6~"), ["up", "down", "pageup", "pagedown"])

    def test_unknown_sequences_are_dropped(self) -> None:
        self.assertEqual(decode_keys("\033[1;5Cx"), ["x"])


class OpenKeyReaderTest(unittest.TestCase):
    def test_no_reader_without_a_terminal(self) -> None:
        self.assertIsNone(open_key_reader(io.StringIO(), lambda key: None))


if __name__ == "__main__":
    unittest.main()
