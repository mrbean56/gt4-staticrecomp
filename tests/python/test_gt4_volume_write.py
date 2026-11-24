"""Hand-packed GT4.VOL regions plus the byte-identity controls on the pinned disc."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import gt4_volume_write as writer

ROOT = Path(__file__).resolve().parents[2]
ISO_PATH = Path(os.environ.get("GT4_ISO", ""))
TOC_OFFSET = writer.TOC_OFFSET
PAGE_BYTES = writer.PAGE_BYTES
DATA_START = TOC_OFFSET + PAGE_BYTES
TAIL_BYTES = 512
PAGE_XOR_KEY = 0x14AC327A


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
    return bytes(page)


def catalogue_bytes(page):
    encoded = raw_deflate(bytes(value ^ 0x55 for value in page), zlib.Z_SYNC_FLUSH)
    stored = bytearray(72)
    struct.pack_into("<IIIIHH", stored, 0, 0xACB990AD, 0x00030001,
                     72 + len(encoded), 1, PAGE_BYTES, 1)
    struct.pack_into("<II", stored, 64, 72 ^ PAGE_XOR_KEY,
                     (72 + len(encoded)) ^ ((2 * PAGE_XOR_KEY) & 0xFFFFFFFF))
    return bytes(stored) + encoded


def build_volume(entries):
    """entries: (name, kind, data_page, decoded, stored)."""
    page_files = [(1, name, kind, data_page, len(stored), len(decoded))
                  for name, kind, data_page, decoded, stored in entries]
    stored_catalogue = catalogue_bytes(leaf_page(page_files))
    page_count = max(entry[2] for entry in entries) + 1
    volume_size = DATA_START + PAGE_BYTES * page_count + TAIL_BYTES
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

    def rewrite(self, replacements=None):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "volume.vol"
            report = writer.rewrite_volume_region(
                io.BytesIO(self.volume), destination, replacements,
                volume_size=len(self.volume),
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
            ({"raw.bin": self.raw + b"!"}, "decoded size"),
            ({"stable.bin": b"\x00" * len(self.stable)}, "stored size"),
            ({"raw.bin": "text"}, "payload bytes"),
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
            destination = Path(temporary) / "output.vol"
            with self.assertRaisesRegex(writer.VolumeWriteError, "pinned USA v2.00"):
                writer.rewrite_volume(source, destination)
            self.assertFalse(destination.exists())

    def test_catalogue_rejects_unknown_page_framing(self):
        size = struct.unpack_from("<I", self.volume, TOC_OFFSET + 8)[0]
        stored = self.volume[TOC_OFFSET:TOC_OFFSET + size]
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


@unittest.skipUnless(
    bool(os.environ.get("GT4_ISO")) and ISO_PATH.is_file(),
    "set GT4_ISO to the pinned GT4 USA v2.00 ISO to run the byte-identity controls",
)
class RealDiscTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with ISO_PATH.open("rb") as stream:
            stream.seek(writer.VOLUME_OFFSET)
            cls.source_sha256 = hash_stream(stream, writer.VOLUME_SIZE)

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
        try:
            import gt4_volume
        except ImportError as error:
            self.skipTest(f"read-only reader is unavailable here: {error}")
        with ISO_PATH.open("rb") as stream:
            theirs = gt4_volume.read_catalogue(stream)
            ours = writer.read_catalogue(stream, writer.VOLUME_OFFSET, writer.VOLUME_SIZE)
        self.assertEqual(sorted(theirs.files), sorted(ours.files))
        for path, mine in ours.files.items():
            other = theirs.files[path]
            self.assertEqual(
                (mine.offset, mine.kind, mine.stored_size, mine.size),
                (other.offset, other.kind, other.stored_size, other.size),
                path,
            )

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


if __name__ == "__main__":
    unittest.main()
