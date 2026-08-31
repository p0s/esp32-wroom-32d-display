import hashlib
import struct
import unittest

import inspect_factory


class FactoryInspectorTests(unittest.TestCase):
    def test_parses_and_hashes_partition(self):
        flash = bytearray(b"\xff" * (16 * 1024 * 1024))
        contents = b"factory-data" * 100
        offset = 0x9000
        flash[offset : offset + len(contents)] = contents
        entry = struct.pack(
            "<HBBLL16sL",
            inspect_factory.PARTITION_MAGIC,
            1,
            2,
            offset,
            len(contents),
            b"nvs\0".ljust(16, b"\0"),
            0,
        )
        start = inspect_factory.PARTITION_TABLE_OFFSET
        flash[start : start + len(entry)] = entry

        partitions = inspect_factory.parse_partitions(bytes(flash))

        self.assertEqual(len(partitions), 1)
        self.assertEqual(partitions[0]["label"], "nvs")
        self.assertEqual(partitions[0]["subtype"], "nvs")
        self.assertEqual(partitions[0]["offsetHex"], "0x9000")
        self.assertEqual(partitions[0]["sha256"], hashlib.sha256(contents).hexdigest())

    def test_rejects_partition_outside_image(self):
        flash = bytearray(b"\xff" * (16 * 1024 * 1024))
        entry = struct.pack(
            "<HBBLL16sL",
            inspect_factory.PARTITION_MAGIC,
            0,
            0,
            len(flash) - 10,
            20,
            b"factory\0".ljust(16, b"\0"),
            0,
        )
        start = inspect_factory.PARTITION_TABLE_OFFSET
        flash[start : start + len(entry)] = entry

        with self.assertRaisesRegex(ValueError, "beyond flash image"):
            inspect_factory.parse_partitions(bytes(flash))


if __name__ == "__main__":
    unittest.main()
