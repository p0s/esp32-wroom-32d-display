import unittest

import capture_runtime_credential as capture


class CaptureRuntimeCredentialTests(unittest.TestCase):
    def test_parses_exact_owner_boot_line(self):
        result = capture.parse_boot_line(
            "control AP=imDisplay-CE8C password=XS23456789ABCD ip=0.0.0.0 ready=0"
        )
        self.assertEqual(result["accessPoint"], "imDisplay-CE8C")
        self.assertEqual(result["address"], "0.0.0.0")
        self.assertFalse(result["ready"])

    def test_rejects_unrelated_or_short_secret_lines(self):
        self.assertIsNone(capture.parse_boot_line("budget update: 3 windows"))
        self.assertIsNone(
            capture.parse_boot_line("control AP=A password=short ip=192.168.4.1 ready=1")
        )


if __name__ == "__main__":
    unittest.main()
