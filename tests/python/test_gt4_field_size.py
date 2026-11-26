"""Controls for the six-car field expansion tool (TASK-219)."""

from __future__ import annotations

import os
import struct
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import gt4_field_size as field_size
import gt4_specdb as specdb

ISO = Path(os.environ.get("GT4_ISO", ""))
IMAGE = Path(os.environ.get("GT4_FIELD_IMAGE", ""))


def table_of(sizes: list[int]) -> specdb.Table:
    rows = [specdb.Row(1000 + index, bytes([0x11]) * 0x3E + bytes([size]) + bytes([0x22]) * 8)
            for index, size in enumerate(sizes)]
    return specdb.Table(row_count=len(rows), row_length=0x3E + 1 + 8, compressed=False,
                        rows=rows, version=0x00080001)


class RaiseTests(unittest.TestCase):
    def test_only_six_car_rows_change(self):
        table = table_of([6, 2, 6, 1, 4])
        encoded, changes = field_size.raise_fields(table, 24)
        self.assertEqual(changes, [(1000, 6, 24), (1002, 6, 24)])
        rewritten = specdb.parse_table(encoded)
        self.assertEqual([row.data[0x3E] for row in rewritten.rows], [24, 2, 24, 1, 4])
        self.assertEqual([row.row_id for row in rewritten.rows], [1000, 1001, 1002, 1003, 1004])

    def test_every_other_byte_is_unchanged(self):
        table = table_of([6])
        encoded, _ = field_size.raise_fields(table, 24)
        before = table.rows[0].data
        after = specdb.parse_table(encoded).rows[0].data
        self.assertEqual(len(before), len(after))
        self.assertEqual([index for index, (a, b) in enumerate(zip(before, after)) if a != b],
                         [0x3E])

    def test_raising_twice_changes_nothing_the_second_time(self):
        encoded, _ = field_size.raise_fields(table_of([6]), 24)
        with self.assertRaises(field_size.FieldSizeError):
            field_size.raise_fields(specdb.parse_table(encoded), 24)

    def test_target_must_exceed_source(self):
        with self.assertRaises(field_size.FieldSizeError):
            field_size.raise_fields(table_of([6]), 6)
        with self.assertRaises(field_size.FieldSizeError):
            field_size.raise_fields(table_of([6]), 4)

    def test_a_table_without_the_source_field_is_refused(self):
        with self.assertRaises(field_size.FieldSizeError):
            field_size.raise_fields(table_of([2, 1]), 24)

    def test_pool_is_cycled_up_to_the_field_size(self):
        entries = [bytes([value]) * 16 for value in (1, 2, 3)]
        pool = struct.pack("<II", 3, 0) + b"".join(entries)
        raised = field_size.raise_pool(pool, 24)
        count, reserved = struct.unpack_from("<II", raised, 0)
        self.assertEqual((count, reserved), (23, 0))
        self.assertEqual(len(raised), 8 + 23 * 16)
        self.assertEqual([raised[8 + index * 16] for index in range(23)],
                         [1, 2, 3] * 7 + [1, 2])

    def test_pool_big_enough_or_empty_is_left_alone(self):
        self.assertIsNone(field_size.raise_pool(struct.pack("<II", 23, 0) + bytes(23 * 16), 24))
        self.assertIsNone(field_size.raise_pool(struct.pack("<II", 0, 0), 24))

    def test_short_rows_are_refused(self):
        table = specdb.Table(row_count=1, row_length=0x20, compressed=False,
                             rows=[specdb.Row(1, bytes(0x20))])
        with self.assertRaises(field_size.FieldSizeError):
            field_size.raise_fields(table, 24)


@unittest.skipUnless(ISO.is_file(), "set GT4_ISO to the pinned USA v2.00 image")
class RealDiscTests(unittest.TestCase):
    def test_shipped_field_histogram(self):
        report = field_size.inspect(ISO)
        self.assertEqual(report["races"], 709)
        self.assertEqual(report["row_length"], 151)
        self.assertEqual(report["field_sizes"], {1: 85, 2: 86, 4: 1, 6: 537})
        self.assertEqual(report["six_car_pools"], {"smaller_than_six": 196,
                                                   "six_to_23": 292, "24_or_more": 49})

    def test_every_six_car_row_can_be_raised(self):
        import gt4_volume

        with gt4_volume.verified_volume(ISO) as stream:
            catalogue = gt4_volume.read_catalogue(stream)
            record = catalogue.files[field_size.RACE_TABLE]
            raw = gt4_volume.read_at(
                stream, gt4_volume.VOLUME_OFFSET + record.offset, record.stored_size,
                gt4_volume.VOLUME_OFFSET + gt4_volume.VOLUME_SIZE)
            table = specdb.parse_table(gt4_volume.decode_payload(raw, record.kind, record.size))
        encoded, changes = field_size.raise_fields(table, 24)
        self.assertEqual(len(changes), 537)
        rewritten = specdb.parse_table(encoded)
        self.assertEqual(field_size.field_sizes(rewritten), {1: 85, 2: 86, 4: 1, 24: 537})
        self.assertEqual([row.row_id for row in rewritten.rows], [row.row_id for row in table.rows])


@unittest.skipUnless(IMAGE.is_file(), "set GT4_FIELD_IMAGE to an image built by this tool")
class RebuiltImageTests(unittest.TestCase):
    def test_the_rebuilt_image_raises_every_six_car_field(self):
        import gt4_volume

        with gt4_volume.rebuilt_volume(IMAGE) as stream:
            catalogue = gt4_volume.read_catalogue(stream, pinned=False)
            record = catalogue.files[field_size.RACE_TABLE]
            raw = gt4_volume.read_at(
                stream, gt4_volume.VOLUME_OFFSET + record.offset, record.stored_size,
                gt4_volume.VOLUME_OFFSET + gt4_volume.VOLUME_SIZE)
            table = specdb.parse_table(gt4_volume.decode_payload(raw, record.kind, record.size))
        self.assertEqual(field_size.field_sizes(table), {1: 85, 2: 86, 4: 1, 24: 537})


if __name__ == "__main__":
    unittest.main()
