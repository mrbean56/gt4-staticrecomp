"""Hand-packed GT4.VOL regions, the page encoder, resized replacements, pinned disc."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import random
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import gt4_specdb
import gt4_volume_write as writer

ROOT = Path(__file__).resolve().parents[2]
ISO_PATH = Path(os.environ.get("GT4_ISO", ""))
REBUILT_ISO = os.environ.get("GT4_REBUILT_ISO")
TOC_OFFSET = writer.TOC_OFFSET
PAGE_BYTES = writer.PAGE_BYTES
DATA_START = TOC_OFFSET + PAGE_BYTES
TAIL_BYTES = 512
PAGE_XOR_KEY = 0x14AC327A
GENERIC_CAR = "specdb/GT4_US2560/GENERIC_CAR.dbt"
GENERIC_CAR_DECODED = 17_960
GENERIC_CAR_UNCOMPRESSED = 23_120
GENERIC_CAR_ROW_59 = "b00a00002400000078320000d107030d0d00000000000000"


def raw_deflate(data, flush=zlib.Z_FINISH):
    encoder = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return encoder.compress(data) + encoder.flush(flush)


def ps2zip_level_one(data):
    """Valid PS2ZIP framing that differs from the writer's level-9 re-framing."""
    encoder = zlib.compressobj(1, zlib.DEFLATED, -zlib.MAX_WBITS)
    compressed = encoder.compress(data) + encoder.flush(zlib.Z_FINISH)
    return struct.pack("<II", writer.PS2ZIP_MAGIC, (-len(data)) & 0xFFFFFFFF) + compressed


def leaf_page(files):
    """One RoFS leaf page: the root directory plus (parent, name, kind, page, stored, size)."""
    page = bytearray(PAGE_BYTES)
    struct.pack_into("<HHii", page, 0, 0, 2 * (len(files) + 1), -1, -1)
    cursor = 12
    table = []

    def put(key, meta):
        nonlocal cursor
        table.append((cursor, len(key), cursor + len(key), len(meta)))
        page[cursor:cursor + len(key)] = key
        cursor += len(key)
        page[cursor:cursor + len(meta)] = meta
        cursor += len(meta)

    put(b"\0\0\0\0.", b"\0" + struct.pack("<I", 1))
    for parent, name, kind, data_page, stored_size, size in files:
        meta = bytes([kind]) + struct.pack("<III", data_page, 0, stored_size)
        if kind == 2:
            meta += struct.pack("<I", size)
        put(struct.pack(">I", parent) + name.encode("ascii"), meta)
    if cursor > PAGE_BYTES - 4 * len(table) - 4:
        raise AssertionError("fixture page is full")
    for index, fields in enumerate(table):
        struct.pack_into("<HHHH", page, PAGE_BYTES - (index + 1) * 8, *fields)
    struct.pack_into("<HH", page, PAGE_BYTES - len(table) * 8 - 4, 0, (cursor + 3) // 4 * 4)
    return bytes(page)


def index_page(children):
    """One RoFS index page; each child is addressed through its metadata fields."""
    page = bytearray(PAGE_BYTES)
    struct.pack_into("<HHii", page, 0, 1, 2 * len(children) + 1, -1, -1)
    cursor = 12
    table = []
    for index, child in enumerate(children):
        key = b"\0\0\0\0" + bytes([0x61 + index])
        table.append((cursor, len(key), child, 0))
        page[cursor:cursor + len(key)] = key
        cursor += len(key)
    for index, fields in enumerate(table):
        struct.pack_into("<HHHH", page, PAGE_BYTES - (index + 1) * 8, *fields)
    struct.pack_into("<I", page, PAGE_BYTES - len(children) * 8 - 4, 0x1234)
    return bytes(page)


def catalogue_bytes(page):
    encoded = raw_deflate(bytes(value ^ 0x55 for value in page), zlib.Z_SYNC_FLUSH)
    stored = bytearray(72)
    struct.pack_into("<IIIIHH", stored, 0, 0xACB990AD, 0x00030001,
                     72 + len(encoded), 1, PAGE_BYTES, 1)
    struct.pack_into("<II", stored, 64, 72 ^ PAGE_XOR_KEY,
                     (72 + len(encoded)) ^ ((2 * PAGE_XOR_KEY) & 0xFFFFFFFF))
    return bytes(stored) + encoded


def build_volume(entries, page_count=None, tail=TAIL_BYTES):
    """entries: (name, kind, data_page, decoded, stored)."""
    page_files = [(1, name, kind, data_page, len(stored), len(decoded))
                  for name, kind, data_page, decoded, stored in entries]
    stored_catalogue = catalogue_bytes(leaf_page(page_files))
    if page_count is None:
        page_count = max(entry[2] for entry in entries) + 1
    volume_size = DATA_START + PAGE_BYTES * page_count + tail
    volume = bytearray((bytes(range(256)) * (volume_size // 256 + 1))[:volume_size])
    volume[TOC_OFFSET:TOC_OFFSET + len(stored_catalogue)] = stored_catalogue
    for name, kind, data_page, decoded, stored in entries:
        start = DATA_START + PAGE_BYTES * data_page
        volume[start:start + len(stored)] = stored
    return bytes(volume)


def hash_stream(stream, length, chunk=8 * 1024 * 1024):
    hasher = hashlib.sha256()
    remaining = length
    while remaining:
        data = stream.read(min(remaining, chunk))
        if not data:
            raise AssertionError("short read")
        hasher.update(data)
        remaining -= len(data)
    return hasher.hexdigest()


def toc_bytes_of(volume):
    size = struct.unpack_from("<I", volume, TOC_OFFSET + 8)[0]
    return volume[TOC_OFFSET:TOC_OFFSET + size]


class PageEncoderTests(unittest.TestCase):
    def test_leaf_page_round_trips_through_the_encoder(self):
        page = leaf_page([(1, "a.bin", 2, 0, 10, 20), (1, "b.bin", 1, 0, 4, 4)])
        parsed = writer.parse_page_image(page, 4)
        self.assertEqual(parsed.kind, 0)
        self.assertEqual(len(parsed.entries), 3)
        self.assertEqual(writer.encode_page(parsed), page)
        self.assertEqual(writer.decode_stored_page(writer.encode_stored_page(parsed)), page)

    def test_index_page_round_trips_through_the_encoder(self):
        page = index_page((1, 2, 3))
        parsed = writer.parse_page_image(page, 4)
        self.assertEqual(parsed.kind, 1)
        self.assertEqual([entry.meta_offset | entry.meta_size << 16
                          for entry in parsed.entries], [1, 2, 3])
        self.assertEqual(writer.encode_page(parsed), page)
        self.assertEqual(writer.decode_stored_page(writer.encode_stored_page(parsed)), page)

    def test_index_child_out_of_range_is_refused(self):
        with self.assertRaisesRegex(writer.VolumeWriteError, "index child"):
            writer.parse_page_image(index_page((1, 5)), 4)

    def test_reencoded_catalogue_preserves_paths_offsets_kinds_and_sizes(self):
        packed = bytes(range(48)) * 8
        volume = build_volume([
            ("raw.bin", 1, 0, b"raw payload 0001", b"raw payload 0001"),
            ("packed.bin", 2, 1, packed, writer.frame_payload(packed, 2)),
        ])
        stored = toc_bytes_of(volume)
        rebuilt = writer.reencode_catalogue(stored)
        self.assertNotEqual(rebuilt, stored, "re-encoding must not reuse shipped framing")
        before = writer.parse_catalogue(stored, len(volume))
        after = writer.parse_catalogue(rebuilt, len(volume))
        self.assertEqual(sorted(before.files), sorted(after.files))
        for path, record in before.files.items():
            other = after.files[path]
            self.assertEqual((record.kind, record.offset, record.stored_size, record.size),
                             (other.kind, other.offset, other.stored_size, other.size), path)
        self.assertEqual(before.directories, after.directories)
        self.assertEqual(before.physical_pages, after.physical_pages)

    def test_page_with_metadata_outside_the_entry_table_is_refused(self):
        page = bytearray(leaf_page([(1, "a.bin", 1, 0, 4, 4)]))
        struct.pack_into("<HHHH", page, PAGE_BYTES - 8, 12, 40, PAGE_BYTES - 2, 4)
        with self.assertRaises(writer.VolumeWriteError):
            writer.parse_page_image(bytes(page), 4)


class SyntheticVolumeTests(unittest.TestCase):
    def setUp(self):
        self.raw = b"raw payload 0001"
        self.stable = bytes(range(48)) * 8
        self.stable_stored = ps2zip_level_one(self.stable)
        self.change = bytes(range(64)) * 8
        self.change_stored = writer.frame_payload(self.change, 2)
        self.change_replacement = bytes([self.change[0] ^ 0xFF]) + self.change[1:]
        if writer.frame_payload(self.stable, 2) == self.stable_stored:
            raise AssertionError("fixture needs a different PS2ZIP encoding")
        if len(writer.frame_payload(self.change_replacement, 2)) != len(self.change_stored):
            raise AssertionError("fixture replacement must keep the stored size")
        self.volume = build_volume([
            ("raw.bin", 1, 0, self.raw, self.raw),
            ("stable.bin", 2, 1, self.stable, self.stable_stored),
            ("change.bin", 2, 2, self.change, self.change_stored),
        ])

    def rewrite(self, replacements=None, volume=None):
        volume = self.volume if volume is None else volume
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "volume.vol"
            report = writer.rewrite_volume_region(
                io.BytesIO(volume), destination, replacements,
                volume_size=len(volume),
            )
            return report, destination.read_bytes()

    def entry(self, report, path):
        return next(entry for entry in report.entries if entry.path == path)

    def test_noop_rewrite_reproduces_every_byte(self):
        report, written = self.rewrite()
        self.assertEqual(written, self.volume)
        self.assertEqual(report.volume_size, len(self.volume))
        self.assertTrue(report.identical)
        self.assertEqual(report.source_sha256, hashlib.sha256(self.volume).hexdigest())
        self.assertEqual(report.written_sha256, report.source_sha256)
        self.assertFalse(any(entry.replaced or entry.reused_stored for entry in report.entries))

    def test_report_lists_offsets_sizes_and_kinds_in_volume_order(self):
        report, _ = self.rewrite()
        self.assertEqual([entry.path for entry in report.entries],
                         ["raw.bin", "stable.bin", "change.bin"])
        self.assertEqual(
            [(entry.kind, entry.offset, entry.stored_size, entry.size)
             for entry in report.entries],
            [(1, DATA_START, len(self.raw), len(self.raw)),
             (2, DATA_START + PAGE_BYTES, len(self.stable_stored), len(self.stable)),
             (2, DATA_START + 2 * PAGE_BYTES, len(self.change_stored), len(self.change))],
        )
        self.assertEqual([(entry.source_offset, entry.source_stored_size, entry.source_size)
                          for entry in report.entries],
                         [(entry.offset, entry.stored_size, entry.size)
                          for entry in report.entries])

    def test_raw_replacement_changes_only_its_bytes(self):
        replacement = b"raw payload 0002"
        report, written = self.rewrite({"raw.bin": replacement})
        entry = self.entry(report, "raw.bin")
        self.assertTrue(entry.replaced)
        self.assertFalse(entry.reused_stored)
        self.assertFalse(report.identical)
        self.assertEqual(len(written), len(self.volume))
        self.assertEqual(written[:DATA_START], self.volume[:DATA_START])
        self.assertEqual(written[DATA_START:DATA_START + len(replacement)], replacement)
        self.assertEqual(written[DATA_START + len(replacement):],
                         self.volume[DATA_START + len(replacement):])

    def test_ps2zip_identity_replacement_reuses_the_stored_bytes(self):
        report, written = self.rewrite({"stable.bin": self.stable})
        entry = self.entry(report, "stable.bin")
        self.assertTrue(entry.replaced)
        self.assertTrue(entry.reused_stored)
        self.assertTrue(report.identical)
        self.assertEqual(written, self.volume)

    def test_ps2zip_changed_replacement_fits_the_recorded_size(self):
        report, written = self.rewrite({"change.bin": self.change_replacement})
        entry = self.entry(report, "change.bin")
        self.assertTrue(entry.replaced)
        self.assertFalse(entry.reused_stored)
        self.assertFalse(report.identical)
        start = DATA_START + 2 * PAGE_BYTES
        stored = written[start:start + entry.stored_size]
        self.assertEqual(writer.decode_payload(stored, entry.kind, entry.size),
                         self.change_replacement)
        self.assertEqual(written[:start], self.volume[:start])
        self.assertEqual(written[start + entry.stored_size:],
                         self.volume[start + entry.stored_size:])

    def test_growing_first_entry_relocates_the_entries_after_it(self):
        replacement = bytes(random.Random(2160).randbytes(3000))
        volume = build_volume([
            ("raw.bin", 1, 0, self.raw, self.raw),
            ("stable.bin", 2, 1, self.stable, self.stable_stored),
            ("change.bin", 2, 2, self.change, self.change_stored),
        ], page_count=6)
        report, written = self.rewrite({"raw.bin": replacement}, volume)
        raw = self.entry(report, "raw.bin")
        stable = self.entry(report, "stable.bin")
        change = self.entry(report, "change.bin")
        self.assertEqual((raw.offset, raw.stored_size, raw.size),
                         (DATA_START, len(replacement), len(replacement)))
        self.assertEqual((raw.source_offset, raw.source_stored_size), (DATA_START, len(self.raw)))
        self.assertEqual(stable.offset, DATA_START + 4096)
        self.assertEqual(change.offset, DATA_START + 6144)
        self.assertEqual([stable.stored_size, change.stored_size],
                         [len(self.stable_stored), len(self.change_stored)])
        reopened = writer.parse_catalogue(toc_bytes_of(written), len(written))
        self.assertEqual(sorted(reopened.files), ["change.bin", "raw.bin", "stable.bin"])
        self.assertEqual((reopened.files["raw.bin"].offset, reopened.files["raw.bin"].size),
                         (DATA_START, 3000))
        self.assertEqual(reopened.files["stable.bin"].offset, stable.offset)
        self.assertEqual(reopened.files["change.bin"].offset, change.offset)
        self.assertEqual(
            writer.decode_payload(
                written[stable.offset:stable.offset + stable.stored_size], 2, len(self.stable)),
            self.stable)
        self.assertEqual(
            writer.decode_payload(
                written[change.offset:change.offset + change.stored_size], 2, len(self.change)),
            self.change)
        source = writer.parse_catalogue(toc_bytes_of(volume), len(volume))
        self.assertNotEqual(reopened.stored_sha256, source.stored_sha256)

    def test_growing_compressed_entry_rewrites_its_metadata(self):
        replacement = bytes(random.Random(2161).randbytes(5000))
        volume = build_volume([
            ("raw.bin", 1, 0, self.raw, self.raw),
            ("stable.bin", 2, 1, self.stable, self.stable_stored),
            ("change.bin", 2, 2, self.change, self.change_stored),
        ], page_count=6)
        report, written = self.rewrite({"change.bin": replacement}, volume)
        change = self.entry(report, "change.bin")
        self.assertEqual(change.offset, DATA_START + 2 * PAGE_BYTES)
        self.assertEqual(change.size, 5000)
        self.assertGreater(change.stored_size, len(self.change_stored))
        reopened = writer.parse_catalogue(toc_bytes_of(written), len(written))
        self.assertEqual((reopened.files["change.bin"].size, reopened.files["change.bin"].kind),
                         (5000, 2))
        self.assertEqual(
            writer.decode_payload(written[change.offset:change.offset + change.stored_size],
                                  2, 5000), replacement)
        self.assertEqual(reopened.files["raw.bin"].offset, DATA_START)
        self.assertEqual(reopened.files["stable.bin"].offset, DATA_START + PAGE_BYTES)

    def test_shrinking_entry_leaves_the_next_offsets_alone(self):
        replacement = b"short"
        report, written = self.rewrite({"raw.bin": replacement})
        self.assertEqual(self.entry(report, "raw.bin").stored_size, len(replacement))
        self.assertEqual(self.entry(report, "stable.bin").offset, DATA_START + PAGE_BYTES)
        self.assertEqual(self.entry(report, "change.bin").offset, DATA_START + 2 * PAGE_BYTES)
        self.assertEqual(written[DATA_START + len(replacement):DATA_START + PAGE_BYTES],
                         self.volume[DATA_START + len(replacement):DATA_START + PAGE_BYTES])

    def test_layout_that_does_not_fit_the_extent_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "volume.vol"
            with self.assertRaisesRegex(writer.VolumeWriteError, "does not fit"):
                writer.rewrite_volume_region(
                    io.BytesIO(self.volume), destination,
                    {"raw.bin": bytes(6000)}, volume_size=len(self.volume))
            self.assertFalse(destination.exists())

    def test_frame_payload_round_trips_both_kinds(self):
        for kind in (1, 2):
            with self.subTest(kind=kind):
                framed = writer.frame_payload(self.change, kind)
                self.assertEqual(writer.decode_payload(framed, kind, len(self.change)),
                                 self.change)
        self.assertEqual(writer.frame_payload(self.change, 1), self.change)

    def test_refusals_leave_no_output(self):
        cases = (
            ({"absent.bin": self.raw}, "absent"),
            ({"raw.bin": "text"}, "payload bytes"),
            ({"raw.bin": b"x" * (writer.FILE_CAP + 1)}, "file cap"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "volume.vol"
            for replacements, message in cases:
                with self.subTest(message=message), \
                        self.assertRaisesRegex(writer.VolumeWriteError, message):
                    writer.rewrite_volume_region(
                        io.BytesIO(self.volume), destination, replacements,
                        volume_size=len(self.volume),
                    )
                self.assertFalse(destination.exists())

    def test_overlapping_entries_are_refused(self):
        volume = build_volume([
            ("a.bin", 1, 0, b"abcd", b"abcd"),
            ("b.bin", 1, 0, b"abcdefgh", b"abcdefgh"),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "volume.vol"
            with self.assertRaisesRegex(writer.VolumeWriteError, "overlap"):
                writer.rewrite_volume_region(io.BytesIO(volume), destination,
                                             volume_size=len(volume))
            self.assertFalse(destination.exists())

    def test_unpinned_disc_is_refused_before_reading(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "wrong.iso"
            source.write_bytes(b"wrong")
            destination = Path(temporary) / "output.iso"
            with self.assertRaisesRegex(writer.VolumeWriteError, "pinned USA v2.00"):
                writer.rewrite_iso(source, destination)
            self.assertFalse(destination.exists())

    def test_catalogue_rejects_unknown_page_framing(self):
        stored = toc_bytes_of(self.volume)
        size = len(stored)
        extended = bytearray(stored) + b"x"
        struct.pack_into("<I", extended, 8, size + 1)
        struct.pack_into("<I", extended, 64 + 4,
                         (size + 1) ^ ((2 * PAGE_XOR_KEY) & 0xFFFFFFFF))
        with self.assertRaises(writer.VolumeWriteError):
            writer.parse_catalogue(bytes(extended), len(self.volume))
        damaged = bytearray(stored)
        damaged[64] ^= 1
        with self.assertRaises(writer.VolumeWriteError):
            writer.parse_catalogue(bytes(damaged), len(self.volume))

    def test_catalogue_that_no_longer_fits_its_pages_is_refused(self):
        stored = toc_bytes_of(self.volume)
        pages = writer.parse_page_images(stored, 1) * 2
        with self.assertRaisesRegex(writer.VolumeWriteError, "allocated pages"):
            writer.encode_catalogue(pages, [bytes(writer.MAX_STORED_PAGE)] * 2, 1)


def require_pycdlib(test):
    try:
        import gt4_volume
    except ImportError as error:
        test.skipTest(f"read-only reader is unavailable here: {error}")
    return gt4_volume


@unittest.skipUnless(
    bool(os.environ.get("GT4_ISO")) and ISO_PATH.is_file(),
    "set GT4_ISO to the pinned GT4 USA v2.00 ISO to run the real-disc controls",
)
class RealDiscTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with ISO_PATH.open("rb") as stream:
            stream.seek(writer.VOLUME_OFFSET)
            cls.source_sha256 = hash_stream(stream, writer.VOLUME_SIZE)

    def catalogue_and_toc(self):
        with ISO_PATH.open("rb") as stream:
            header = writer.read_at(stream, writer.VOLUME_OFFSET + writer.TOC_OFFSET, 64,
                                    writer.VOLUME_OFFSET + writer.VOLUME_SIZE)
            size = struct.unpack_from("<I", header, 8)[0]
            stored = writer.read_at(stream, writer.VOLUME_OFFSET + writer.TOC_OFFSET, size,
                                    writer.VOLUME_OFFSET + writer.VOLUME_SIZE)
            catalogue = writer.read_catalogue(stream, writer.VOLUME_OFFSET, writer.VOLUME_SIZE)
        return catalogue, stored

    def test_noop_rewrite_matches_the_pinned_region(self):
        pin = json.loads(
            (ROOT / "docs/inputs/slice119-nenkai-assets.json").read_text(encoding="utf-8")
        )["catalogue"]
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "gt4-noop.vol"
            report = writer.rewrite_volume(ISO_PATH, destination)
            self.assertEqual(destination.stat().st_size, writer.VOLUME_SIZE)
            with destination.open("rb") as stream:
                written = hash_stream(stream, writer.VOLUME_SIZE)
                self.assertEqual(stream.read(1), b"")
        self.assertTrue(report.identical)
        self.assertEqual(report.source_sha256, self.source_sha256)
        self.assertEqual(report.written_sha256, self.source_sha256)
        self.assertEqual(written, self.source_sha256)
        self.assertEqual(report.catalogue_sha256, pin["catalogue_input_window_sha256"])
        self.assertEqual(report.decoded_catalogue_sha256, pin["decoded_catalogue_sha256"])
        self.assertEqual(len(report.entries), pin["catalogue_file_count"])
        self.assertFalse(any(entry.replaced or entry.reused_stored
                             for entry in report.entries))

    def test_writer_catalogue_matches_the_read_only_reader(self):
        """The writer duplicates the reader; both must see one catalogue."""
        volume = require_pycdlib(self)
        with ISO_PATH.open("rb") as stream:
            theirs = volume.read_catalogue(stream)
            ours = writer.read_catalogue(stream, writer.VOLUME_OFFSET, writer.VOLUME_SIZE)
        self.assertEqual(sorted(theirs.files), sorted(ours.files))
        for path, mine in ours.files.items():
            other = theirs.files[path]
            self.assertEqual(
                (mine.offset, mine.kind, mine.stored_size, mine.size),
                (other.offset, other.kind, other.stored_size, other.size),
                path,
            )

    def test_whole_catalogue_reencodes_from_its_decoded_pages(self):
        catalogue, stored = self.catalogue_and_toc()
        rebuilt = writer.reencode_catalogue(stored)
        self.assertNotEqual(rebuilt, stored)
        after = writer.parse_catalogue(rebuilt, writer.VOLUME_SIZE)
        self.assertEqual(len(after.files), 18_352)
        self.assertEqual(sorted(after.files), sorted(catalogue.files))
        self.assertEqual(after.directories, catalogue.directories)
        self.assertEqual(after.records, catalogue.records)
        for path, record in catalogue.files.items():
            other = after.files[path]
            self.assertEqual((record.kind, record.offset, record.stored_size, record.size),
                             (other.kind, other.offset, other.stored_size, other.size), path)
        # The read-only reader parses the re-encoded catalogue the same way.
        volume = require_pycdlib(self)
        theirs = volume.parse_catalogue(rebuilt, volume.VOLUME_SIZE)
        self.assertEqual(sorted(theirs.files), sorted(catalogue.files))
        for path, record in catalogue.files.items():
            other = theirs.files[path]
            self.assertEqual((record.kind, record.offset, record.stored_size, record.size),
                             (other.kind, other.offset, other.stored_size, other.size), path)

    def test_unchanged_payload_as_explicit_replacement_is_byte_identical(self):
        with ISO_PATH.open("rb") as stream:
            catalogue = writer.read_catalogue(stream, writer.VOLUME_OFFSET, writer.VOLUME_SIZE)
            selected = None
            for path, record in sorted(catalogue.files.items(),
                                       key=lambda item: item[1].offset):
                if record.kind != 2 or record.stored_size > writer.FILE_CAP:
                    continue
                if record.size > writer.FILE_CAP:
                    continue
                stored = writer.read_at(stream, writer.VOLUME_OFFSET + record.offset,
                                        record.stored_size,
                                        writer.VOLUME_OFFSET + writer.VOLUME_SIZE)
                decoded = writer.decode_payload(stored, record.kind, record.size)
                if writer.frame_payload(decoded, record.kind) != stored:
                    selected = (path, decoded)
                    break
        self.assertIsNotNone(selected, "no compressed entry needed the stored-bytes fallback")
        path, decoded = selected
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "gt4-identity.vol"
            report = writer.rewrite_volume(ISO_PATH, destination, {path: decoded})
            with destination.open("rb") as stream:
                written = hash_stream(stream, writer.VOLUME_SIZE)
        entry = next(entry for entry in report.entries if entry.path == path)
        self.assertTrue(entry.replaced)
        self.assertTrue(entry.reused_stored)
        self.assertTrue(report.identical)
        self.assertEqual(written, self.source_sha256)

    def uncompressed_table(self):
        with ISO_PATH.open("rb") as stream:
            catalogue = writer.read_catalogue(stream, writer.VOLUME_OFFSET, writer.VOLUME_SIZE)
            record = catalogue.files[GENERIC_CAR]
            stored = writer.read_at(stream, writer.VOLUME_OFFSET + record.offset,
                                    record.stored_size, writer.VOLUME_OFFSET + writer.VOLUME_SIZE)
            decoded = writer.decode_payload(stored, record.kind, record.size)
        self.assertEqual(len(decoded), GENERIC_CAR_DECODED)
        table = gt4_specdb.parse_table(decoded)
        written = gt4_specdb.write_table(table)
        self.assertEqual(len(written), GENERIC_CAR_UNCOMPRESSED)
        return written

    def test_resized_specdb_table_end_to_end(self):
        table = self.uncompressed_table()
        temporary = None
        if REBUILT_ISO:
            rebuilt = Path(REBUILT_ISO)
            self.assertTrue(rebuilt.is_file(), "GT4_REBUILT_ISO must name the built image")
        else:
            temporary = tempfile.TemporaryDirectory()
            rebuilt = Path(temporary.name) / "gt4-rebuilt.iso"
            writer.rewrite_iso(ISO_PATH, rebuilt, {GENERIC_CAR: table})
        try:
            self.verify_rebuilt_iso(rebuilt, table)
        finally:
            if temporary is not None:
                temporary.cleanup()

    def verify_rebuilt_iso(self, rebuilt, table):
        self.assertEqual(rebuilt.stat().st_size, ISO_PATH.stat().st_size)
        with ISO_PATH.open("rb") as left, rebuilt.open("rb") as right:
            source = writer.read_catalogue(left, writer.VOLUME_OFFSET, writer.VOLUME_SIZE)
            built = writer.read_catalogue(right, writer.VOLUME_OFFSET, writer.VOLUME_SIZE)
            self.assertEqual(sorted(built.files), sorted(source.files))
            self.assertEqual(len(built.files), 18_352)
            self.assertEqual(built.data_start, source.data_start)
            self.assertEqual(built.physical_pages, source.physical_pages)
            self.assertNotEqual(built.stored_sha256, source.stored_sha256)
            replaced = built.files[GENERIC_CAR]
            self.assertEqual((replaced.kind, replaced.size), (2, len(table)))
            decoded = writer.decode_payload(
                writer.read_at(right, writer.VOLUME_OFFSET + replaced.offset,
                               replaced.stored_size, writer.VOLUME_OFFSET + writer.VOLUME_SIZE),
                replaced.kind, replaced.size)
            self.assertEqual(decoded, table)
            parsed = gt4_specdb.parse_table(decoded)
            self.assertEqual((parsed.row_count, parsed.row_length), (722, 24))
            row = next(row for row in parsed.rows if row.row_id == 59)
            self.assertEqual(row.data.hex(), GENERIC_CAR_ROW_59)
            for path, record in source.files.items():
                if path == GENERIC_CAR:
                    continue
                mine = built.files[path]
                self.assertEqual((mine.kind, mine.size), (record.kind, record.size), path)
                self.assertEqual(mine.stored_size, record.stored_size, path)
                before = writer.read_at(left, writer.VOLUME_OFFSET + record.offset,
                                        record.stored_size,
                                        writer.VOLUME_OFFSET + writer.VOLUME_SIZE)
                after = writer.read_at(right, writer.VOLUME_OFFSET + mine.offset,
                                       mine.stored_size,
                                       writer.VOLUME_OFFSET + writer.VOLUME_SIZE)
                self.assertEqual(hashlib.sha256(before).digest(),
                                 hashlib.sha256(after).digest(), path)
                if record.stored_size <= writer.FILE_CAP:
                    self.assertEqual(
                        writer.decode_payload(after, mine.kind, mine.size),
                        writer.decode_payload(before, record.kind, record.size), path)
        try:
            import gt4_volume
        except ImportError:
            return
        # The read-only reader opens the rebuilt image through its own path.
        with gt4_volume.rebuilt_volume(rebuilt) as stream:
            theirs = gt4_volume.read_catalogue(stream, pinned=False)
        self.assertEqual(sorted(theirs.files), sorted(source.files))
        for path, record in source.files.items():
            if path == GENERIC_CAR:
                continue
            other = theirs.files[path]
            self.assertEqual((record.kind, record.offset, record.stored_size, record.size),
                             (other.kind, other.offset, other.stored_size, other.size), path)


if __name__ == "__main__":
    unittest.main()
