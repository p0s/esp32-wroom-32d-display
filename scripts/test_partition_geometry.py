import csv
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PartitionGeometryTests(unittest.TestCase):
    def test_factory_ota_slots_remain_exact_and_shared(self):
        with (ROOT / "firmware/partitions.csv").open(newline="", encoding="utf-8") as source:
            rows = {
                row[0].strip(): (int(row[3].strip(), 0), int(row[4].strip(), 0))
                for row in csv.reader(line for line in source if not line.startswith("#"))
            }
        self.assertEqual(rows["ota_0"], (0x10000, 0x1F0000))
        self.assertEqual(rows["ota_1"], (0x200000, 0x1F0000))
        self.assertEqual(rows["ota_0"][0] + rows["ota_0"][1], rows["ota_1"][0])

        firmware_ini = (ROOT / "firmware/platformio.ini").read_text(encoding="utf-8")
        bootstrap_ini = (ROOT / "ota-bootstrap/platformio.ini").read_text(encoding="utf-8")
        self.assertIn("board_build.partitions = partitions.csv", firmware_ini)
        self.assertIn("board_build.partitions = ../firmware/partitions.csv", bootstrap_ini)

    def test_both_receivers_bound_ota_to_one_factory_slot(self):
        for relative in ("firmware/src/main.cpp", "ota-bootstrap/src/main.cpp"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("kMaxFirmwareBytes = 0x1f0000", source)


if __name__ == "__main__":
    unittest.main()
