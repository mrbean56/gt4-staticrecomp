"""Controls for growing GT4.VOL inside its disc image (TASK-218)."""

from __future__ import annotations

import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import gt4_iso_grow as grow
import gt4_volume_write as vw

ISO = Path(os.environ.get("GT4_ISO", ""))
SHIPPED_EXTENT = 216_840_192
SHIPPED_SIZE = 2_459_502_592
SHIPPED_SECTORS = 1_317_056


class RealImageTests(unittest.TestCase):
    """One grown image carries every assertion: a grow copies the whole disc."""

    grown: Path | None = None
    directory: tempfile.TemporaryDirectory | None = None
    report: dict = {}
    catalogue = None
    shipped = None

    @classmethod
    def setUpClass(cls):
        if not ISO.is_file():
            raise unittest.SkipTest("GT4_ISO is not set to a disc image")
        cls.directory = tempfile.TemporaryDirectory()
        cls.grown = Path(cls.directory.name) / "grown.iso"
        with ISO.open("rb") as stream:
            cls.shipped = vw.read_catalogue(stream, vw.VOLUME_OFFSET, vw.VOLUME_SIZE)
            donor = cls.shipped.files["crs/deepforest"]
            stored = vw.read_at(stream, vw.VOLUME_OFFSET + donor.offset, donor.stored_size,
                                vw.VOLUME_OFFSET + vw.VOLUME_SIZE)
            payload = vw.decode_payload(stored, donor.kind, donor.size)
        cls.report = grow.grow_image(ISO, cls.grown, 8 * 1024 * 1024, None,
                                     {"crs/zz_probe": payload})
        with cls.grown.open("rb") as stream:
            cls.catalogue = vw.read_catalogue(stream, vw.VOLUME_OFFSET,
                                              cls.report["volume_size"])

    @classmethod
    def tearDownClass(cls):
        if cls.directory is not None:
            cls.directory.cleanup()

    def test_the_shipped_record_is_read_back(self):
        with ISO.open("rb") as stream:
            self.assertEqual(grow.record_extent(stream), (SHIPPED_EXTENT, SHIPPED_SIZE))
            self.assertEqual(grow.read_pvd(stream)[0], SHIPPED_SECTORS)

    def test_the_volume_grows_by_the_requested_bytes(self):
        self.assertEqual(self.report["volume_offset"], SHIPPED_EXTENT)
        self.assertEqual(self.report["volume_size"], SHIPPED_SIZE + 8 * 1024 * 1024)
        self.assertEqual(self.report["growth"], 8 * 1024 * 1024)
        with self.grown.open("rb") as stream:
            _, size = grow.record_extent(stream)
            self.assertEqual(size, self.report["volume_size"])
            space = grow.read_pvd(stream)[0]
            self.assertEqual(space, SHIPPED_SECTORS,
                             "the volume space size moved off its shipped value")
            self.assertEqual(space, self.report["filesystem_sectors"])
            self.assertGreaterEqual(space * 2048, SHIPPED_EXTENT + size)
            record, size_field = grow.volume_record(stream)
            del record
            stream.seek(size_field)
            raw = stream.read(8)
            self.assertEqual(raw, struct.pack("<I", size) + struct.pack(">I", size))

    def test_every_shipped_entry_keeps_its_place(self):
        self.assertEqual(sorted(self.catalogue.files),
                         sorted(set(self.shipped.files) | {"crs/zz_probe"}))
        for path, record in self.shipped.files.items():
            other = self.catalogue.files[path]
            self.assertEqual((record.offset, record.stored_size, record.size),
                             (other.offset, other.stored_size, other.size), path)

    def test_the_addition_lands_in_the_grown_region(self):
        addition = self.catalogue.files["crs/zz_probe"]
        donor_record = self.shipped.files["crs/deepforest"]
        with ISO.open("rb") as stream:
            donor = vw.read_at(stream, vw.VOLUME_OFFSET + donor_record.offset,
                               donor_record.stored_size, vw.VOLUME_OFFSET + vw.VOLUME_SIZE)
        with self.grown.open("rb") as stream:
            stored = vw.read_at(stream, vw.VOLUME_OFFSET + addition.offset, addition.stored_size,
                                vw.VOLUME_OFFSET + self.report["volume_size"])
        self.assertEqual(vw.decode_payload(stored, addition.kind, addition.size),
                         vw.decode_payload(donor, donor_record.kind, donor_record.size),
                         "the appended entry does not repeat its donor")
        self.assertGreater(addition.offset + addition.stored_size, SHIPPED_SIZE,
                           "the appended entry does not reach the grown region")

    def test_the_image_tail_is_copied_unchanged(self):
        tail_start = SHIPPED_EXTENT + self.report["volume_size"]
        with ISO.open("rb") as stream:
            stream.seek(tail_start)
            before = stream.read(1 << 20)
        with self.grown.open("rb") as stream:
            stream.seek(tail_start)
            after = stream.read(1 << 20)
        self.assertEqual(before, after)

    def test_the_grown_tail_is_zero_filled(self):
        addition = self.catalogue.files["crs/zz_probe"]
        start = addition.offset + addition.stored_size
        with self.grown.open("rb") as stream:
            stream.seek(SHIPPED_EXTENT + start)
            filler = stream.read(self.report["volume_size"] - start)
        self.assertEqual(set(filler), {0})


class LimitTests(unittest.TestCase):
    def test_zero_growth_is_refused(self):
        with self.assertRaises(grow.GrowError):
            grow.grow_image(Path("missing"), Path("missing.out"), 0)

    def test_growth_past_the_filesystem_is_refused(self):
        if not ISO.is_file():
            self.skipTest("GT4_ISO is not set to a disc image")
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(grow.GrowError):
                grow.grow_image(ISO, Path(folder) / "none.iso", 64 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
