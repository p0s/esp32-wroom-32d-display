import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import serial_ota


class SerialOtaTests(unittest.TestCase):
    def test_header_binds_size_and_sha256(self):
        digest = "ab" * 32
        self.assertEqual(
            serial_ota.ota_header(123, digest, "v1"),
            f"XSURE_OTA_V1 123 {digest}\n".encode("ascii"),
        )

    def test_v2_header_requests_acknowledged_chunks(self):
        digest = "ab" * 32
        self.assertEqual(
            serial_ota.ota_header(123, digest, "v2"),
            f"XSURE_OTA_V2 123 {digest} 64\n".encode("ascii"),
        )

    def test_header_rejects_oversize_firmware(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            serial_ota.ota_header(serial_ota.MAX_FIRMWARE_BYTES + 1, "ab" * 32, "v2")

    def test_sha256_reads_exact_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.bin"
            path.write_bytes(b"firmware")
            self.assertEqual(serial_ota.sha256(path), hashlib.sha256(b"firmware").hexdigest())

    def test_line_reader_skips_private_boot_output(self):
        uart = mock.MagicMock()
        uart.readline.side_effect = [
            b"control AP=private password=private\n",
            b"ota ready: 123 bytes\n",
        ]
        result = serial_ota.read_matching_line(
            uart,
            lambda line: line == "ota ready: 123 bytes",
            1.0,
        )
        self.assertEqual(result, "ota ready: 123 bytes")

    def test_line_reader_raises_on_device_rejection(self):
        uart = mock.MagicMock()
        uart.readline.return_value = b"ota rejected: sha256 mismatch\n"
        with self.assertRaisesRegex(RuntimeError, "sha256 mismatch"):
            serial_ota.read_matching_line(uart, lambda _line: False, 1.0)

    @mock.patch.object(serial_ota.time, "sleep")
    def test_stream_paces_small_complete_chunks(self, sleep):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.bin"
            path.write_bytes(b"x" * 600)
            uart = mock.MagicMock()
            uart.write.side_effect = lambda block: len(block)
            self.assertEqual(serial_ota.stream_firmware_v1(uart, path), 600)
        self.assertEqual(
            [len(call.args[0]) for call in uart.write.call_args_list],
            [128, 128, 128, 128, 88],
        )
        self.assertEqual(uart.flush.call_count, 5)
        self.assertEqual(sleep.call_count, 5)

    def test_v2_waits_for_each_exact_chunk_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.bin"
            path.write_bytes(b"x" * 150)
            uart = mock.MagicMock()
            uart.write.side_effect = lambda block: len(block)
            uart.readline.side_effect = [
                b"ota chunk: 64\n",
                b"ota chunk: 128\n",
                b"ota chunk: 150\n",
            ]
            self.assertEqual(serial_ota.stream_firmware_v2(uart, path, 64), 150)
        self.assertEqual([len(call.args[0]) for call in uart.write.call_args_list], [64, 64, 22])


if __name__ == "__main__":
    unittest.main()
