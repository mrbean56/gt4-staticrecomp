"""Controls for the per-car CARS record reader and clone rewrite (TASK-222)."""

from __future__ import annotations

import os
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import gt4_car_clone as car_clone
import gt4_car_index as car_index
import gt4_car_map as car_map
import gt4_volume

ISO = Path(os.environ.get("GT4_ISO", ""))
CARS_PREFIX = "specdb/GT4_US2560/CARS/"


def read_payload(iso_path: Path, row_id: int) -> bytes:
    with gt4_volume.rebuilt_volume(iso_path) as stream:
        catalogue = gt4_volume.read_catalogue(stream, pinned=False)
        record = catalogue.files[CARS_PREFIX + f"{row_id:016X}"]
        raw = gt4_volume.read_at(
            stream, gt4_volume.VOLUME_OFFSET + record.offset, record.stored_size,
            gt4_volume.VOLUME_OFFSET + gt4_volume.VOLUME_SIZE)
        return gt4_volume.decode_payload(raw, record.kind, record.size)


class SyntheticTests(unittest.TestCase):
    def test_round_trip(self):
        entries = [car_index.IndexEntry(2109, 1, 0), car_index.IndexEntry(3307, 1, 1)]
        data = car_index.write_index(entries)
        self.assertEqual(len(data), car_index.HEADER_BYTES + 2 * car_index.ENTRY_BYTES)
        self.assertEqual(car_index.parse_index(data), entries)

    def test_rejects_a_short_record(self):
        data = car_index.write_index([car_index.IndexEntry(1, 1, 0)])
        with self.assertRaises(ValueError):
            car_index.parse_index(data[:-1])

    def test_rejects_a_repeated_table_letter(self):
        with self.assertRaises(ValueError):
            car_index.write_index([car_index.IndexEntry(1, 1, 0),
                                   car_index.IndexEntry(2, 1, 0)])


@unittest.skipUnless(ISO.is_file(), "set GT4_ISO to the pinned USA v2.00 image")
class RealDiscTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = car_map.read_specdb(ISO)
        cls.record = car_index.read_index(ISO, 59)

    def test_the_ka_record_lists_its_own_rows(self):
        self.assertEqual(len(self.record), 73)
        self.assertEqual(self.record[0], car_index.IndexEntry(2109, 1, 0))
        self.assertEqual(self.record[-1], car_index.IndexEntry(137, 27, 1))
        rows = car_map.car_rows(self.tables, "ka_01")
        by_table = {}
        for name, hits in rows.items():
            table_id = self.tables[name][0].table_id
            for label, row_id, _ in hits:
                by_table[(table_id, row_id)] = label
        shared = [entry for entry in self.record
                  if (entry.table_id, entry.row_id) not in by_table]
        self.assertEqual(shared, [car_index.IndexEntry(1189, 2, 0),
                                  car_index.IndexEntry(44, 4, 0),
                                  car_index.IndexEntry(14, 5, 0),
                                  car_index.IndexEntry(10, 9, 0),
                                  car_index.IndexEntry(2333, 14, 0),
                                  car_index.IndexEntry(3256, 21, 0)])

    def test_the_ka_record_round_trips_byte_for_byte(self):
        payload = read_payload(ISO, 59)
        self.assertEqual(car_index.write_index(car_index.parse_index(payload)), payload)

    def test_a_clone_rewrites_every_row(self):
        plan = car_clone.plan_clone(self.tables, "ka_01", "ka_02", include_scoped=True)
        cloned = car_index.clone_index(self.record, self.tables, plan, "ka_02")
        self.assertEqual(len(cloned), len(self.record))
        mapping = {}
        for name, triples in plan.items():
            for _, source_row, new_row in triples:
                mapping[(name, source_row)] = new_row
        names = {self.tables[name][0].table_id: name for name in plan}
        for old, new in zip(self.record, cloned):
            self.assertEqual(new.table_id, old.table_id)
            self.assertEqual(new.letter, old.letter)
            name = names.get(old.table_id)
            source = mapping.get((name, old.row_id))
            if source is None:
                self.assertEqual(new.row_id, old.row_id)
            else:
                self.assertEqual(new.row_id, source)
                self.assertNotEqual(new.row_id, old.row_id)

    def test_a_row_without_a_record_is_refused(self):
        with self.assertRaises(ValueError):
            car_index.read_index(ISO, 1078)


if __name__ == "__main__":
    unittest.main()
