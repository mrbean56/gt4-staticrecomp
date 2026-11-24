"""Hand-packed GTDB, GTID and GTST controls plus the real-disc oracle."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import gt4_specdb as specdb

# Optional: a directory holding asset-00..07.bin (the selected USA v2.00 SpecDB
# files). Unset means the real-disc oracle is skipped.
REAL_SPECDB = Path(os.environ.get("GT4_SPECDB_ASSETS", ""))


def encode(symbols, codes):
    bits = []
    for symbol in symbols:
        length, code = codes[symbol]
        bits.extend((code >> bit) & 1 for bit in range(length))
    out = bytearray((len(bits) + 7) // 8)
    for index, bit in enumerate(bits):
        if bit:
            out[index // 8] |= 1 << (index % 8)
    return bytes(out)


def prefix_table(codes):
    table = [(0, 0)] * 256
    ordered = sorted(codes.items(), key=lambda item: (item[1][0], item[1][1]))
    for index in range(256):
        for symbol, (bits, code) in ordered:
            if bits <= 8 and (index & ((1 << bits) - 1)) == code:
                table[index] = (symbol, bits)
                break
    return table


def dictionary(codes):
    prefix = prefix_table(codes)
    prefix_bytes = b"".join(
        struct.pack("<BB", symbol, bits) for symbol, bits in prefix
    )
    definitions = b"".join(
        struct.pack("<BBHI", bits, symbol, 0, code)
        for symbol, (bits, code) in codes.items()
    )
    size = 8 + 512 + len(codes) * 8
    return struct.pack("<II", size, len(codes)) + prefix_bytes + definitions


def compressed_table(row_ids, symbol_rows, row_length, codes, templates):
    streams = bytearray()
    offsets = []
    for symbols in symbol_rows:
        offsets.append(len(streams))
        streams.append(len(symbols))
        streams += encode(symbols, codes)
    header = struct.pack(
        "<4sIII", b"GTDB", 0x00010000, len(row_ids), row_length
    )
    infos = b"".join(
        struct.pack("<II", row_id, offset)
        for row_id, offset in zip(row_ids, offsets)
    )
    block = struct.pack("<II", 8 + len(templates) * row_length, len(templates))
    block += b"".join(templates)
    return header + infos + dictionary(codes) + block + bytes(streams)


def gtst(strings):
    count = len(strings)
    area = bytearray()
    offsets = []
    for value in strings:
        offsets.append(len(area))
        raw = value.encode("latin-1") + b"\x00"
        area += struct.pack("<H", len(raw)) + raw
        if len(area) % 2:
            area += b"\x00"
    header = struct.pack("<4sIII", b"GTST", count, 1, 0)
    table = b"".join(struct.pack("<I", offset) for offset in offsets)
    return header + table + bytes(area)


def gtid(table_id, labels):
    count = len(labels)
    area = bytearray()
    keys = []
    for name, row_id in labels:
        keys.append((len(area), row_id))
        raw = name.encode("latin-1") + b"\x00"
        area += struct.pack("<H", len(raw)) + raw
        if len(area) % 2:
            area += b"\x00"
    header = struct.pack("<4sIII", b"GTID", count, 0, table_id)
    key_bytes = b"".join(
        struct.pack("<II", offset, row_id) for offset, row_id in keys
    )
    return header + key_bytes + bytes(area)


class TableTests(unittest.TestCase):
    def setUp(self):
        self.codes = {
            0x80: (1, 0),
            0x00: (2, 3),
            0x02: (3, 1),
            0xFF: (9, 0x1FD),
        }
        self.templates = [b"\x00\x00"]
        self.data = compressed_table(
            [10, 20],
            [[0x00], [0x80, 0x02, 0xFF]],
            2,
            self.codes,
            self.templates,
        )

    def test_compressed_rows_and_long_code(self):
        table = specdb.parse_table(self.data)
        self.assertTrue(table.compressed)
        self.assertEqual((table.row_count, table.row_length), (2, 2))
        self.assertEqual([row.row_id for row in table.rows], [10, 20])
        self.assertEqual(table.rows[0].data, b"\x00\x00")
        # 0xFF uses the nine-bit code, beyond the eight-bit prefix table.
        self.assertEqual(table.rows[1].data, b"\x00\xff")

    def test_three_code_dictionary(self):
        codes = {0x80: (1, 0), 0x00: (3, 1), 0x01: (3, 5)}
        data = compressed_table(
            [1], [[0x80, 0x01, 0x01]], 1, codes, [b"\x00"]
        )
        table = specdb.parse_table(data)
        self.assertEqual(table.rows[0].data, b"\x01")

    def test_zero_value_patch(self):
        codes = {0x80: (1, 0), 0x00: (3, 1)}
        data = compressed_table(
            [1], [[0x80, 0x00]], 1, codes, [b"\x00"]
        )
        table = specdb.parse_table(data)
        self.assertEqual(table.rows[0].data, b"\x00")

    def test_uncompressed_round_trip(self):
        table = specdb.Table(
            2, 4, False,
            [specdb.Row(7, b"\x01\x02\x03\x04"),
             specdb.Row(9, b"\xff\x00\xff\x00")],
        )
        blob = specdb.write_table(table)
        self.assertEqual(struct.unpack_from("<I", blob, 4)[0] & 1, 1)
        parsed = specdb.parse_table(blob)
        self.assertFalse(parsed.compressed)
        self.assertEqual(
            [(row.row_id, row.data) for row in parsed.rows],
            [(7, b"\x01\x02\x03\x04"), (9, b"\xff\x00\xff\x00")],
        )

    def test_compressed_rows_round_trip(self):
        table = specdb.parse_table(self.data)
        blob = specdb.write_table(table)
        self.assertEqual(struct.unpack_from("<I", blob, 4)[0] & 1, 1)
        parsed = specdb.parse_table(blob)
        self.assertFalse(parsed.compressed)
        self.assertEqual(
            [(row.row_id, row.data) for row in parsed.rows],
            [(row.row_id, row.data) for row in table.rows],
        )

    def test_compressed_write_is_rejected(self):
        with self.assertRaises(specdb.SpecDbError):
            specdb.write_table(specdb.parse_table(self.data), compressed=True)

    def test_bad_magic(self):
        data = bytearray(self.data)
        data[:4] = b"NOPE"
        with self.assertRaises(specdb.SpecDbError):
            specdb.parse_table(bytes(data))

    def test_row_count_exceeds_file(self):
        data = bytearray(self.data)
        struct.pack_into("<I", data, 8, 100_000)
        with self.assertRaises(specdb.SpecDbError):
            specdb.parse_table(bytes(data))

    def test_row_offset_past_end(self):
        data = bytearray(self.data)
        struct.pack_into("<I", data, 16 + 8 + 4, 0xFFFFFFFF)
        with self.assertRaises(specdb.SpecDbError):
            specdb.parse_table(bytes(data))

    def test_truncated_code_stream(self):
        data = compressed_table(
            [1], [[0x80, 0x02, 0xFF]], 2, self.codes, self.templates
        )
        with self.assertRaises(specdb.SpecDbError):
            specdb.parse_table(data[:-1])

    def test_uncompressed_truncated(self):
        table = specdb.Table(
            1, 4, False, [specdb.Row(1, b"\x01\x02\x03\x04")]
        )
        with self.assertRaises(specdb.SpecDbError):
            specdb.parse_table(specdb.write_table(table)[:-1])


class DegenerateTableTests(unittest.TestCase):
    """Shipped tables that carry no code variety and the empty table."""

    def test_empty_table_is_header_plus_two_empty_headers(self):
        data = (struct.pack("<4sIII", b"GTDB", 0x00080001, 0, 65)
                + struct.pack("<II", 520, 0) + struct.pack("<II", 8, 0))
        table = specdb.parse_table(data)
        self.assertEqual((table.row_count, table.row_length, table.rows), (0, 65, []))
        self.assertEqual(specdb.write_table(table), data)

    def test_single_symbol_dictionary_ignores_a_stale_prefix_table(self):
        row_length = 3
        template = b"\x2a\x00\x01"
        header = struct.pack("<4sIII", b"GTDB", 0x00080001, 3, row_length)
        infos = b"".join(struct.pack("<II", 100 + index, index * 2) for index in range(3))
        # A 1-code dictionary ships a prefix table that does not describe its code list.
        prefix = b"\x00\x01\x00\xff" * 128
        definition = struct.pack("<BBHI", 1, 0, 0, 0)
        dictionary = struct.pack("<II", 8 + 512 + 8, 1) + prefix + definition
        block = struct.pack("<II", 8 + row_length, 1) + template
        stream = b"\x01\x00" * 3
        table = specdb.parse_table(header + infos + dictionary + block + stream)
        self.assertEqual([row.data for row in table.rows], [template] * 3)
        self.assertEqual([row.row_id for row in table.rows], [100, 101, 102])
        self.assertEqual(
            [row.data for row in specdb.parse_table(specdb.write_table(table)).rows],
            [template] * 3,
        )


class LabelTests(unittest.TestCase):
    def test_gtid_names_and_alignment(self):
        data = gtid(39, [("ka_01", 59), ("m3_0", 653), ("x", 865)])
        labels = specdb.parse_labels(data)
        self.assertEqual(labels.table_id, 39)
        self.assertEqual(len(labels), 3)
        self.assertEqual(labels[0], specdb.Label("ka_01", 59))
        self.assertEqual(labels[1], specdb.Label("m3_0", 653))
        self.assertEqual(labels[2], specdb.Label("x", 865))
        for index in range(3):
            offset = struct.unpack_from("<I", data, 16 + index * 8)[0]
            self.assertEqual(offset % 2, 0)

    def test_gtst_offsets_are_relative_to_string_area(self):
        strings = specdb.parse_strings(gtst(["Ford Ka `01", "BMW 120d `04", "x"]))
        self.assertEqual(strings, ["Ford Ka `01", "BMW 120d `04", "x"])


@unittest.skipUnless(
    bool(os.environ.get("GT4_SPECDB_ASSETS")) and REAL_SPECDB.is_dir(),
    "set GT4_SPECDB_ASSETS to a directory of asset-0N.bin SpecDB files",
)
class RealDiscTests(unittest.TestCase):
    def test_ka_01_identity(self):
        generic = specdb.parse_table((REAL_SPECDB / "asset-07.bin").read_bytes())
        self.assertTrue(generic.compressed)
        self.assertEqual((generic.row_count, generic.row_length), (722, 24))
        generic_row = next(row for row in generic.rows if row.row_id == 59)
        self.assertEqual(len(generic_row.data), 24)
        self.assertEqual(
            hashlib.sha256(generic_row.data).hexdigest(),
            "17c89b9c155420044472c90e8173a79d479779f7caf86a25474cc8460efa2367",
        )

        names = specdb.parse_table((REAL_SPECDB / "asset-01.bin").read_bytes())
        self.assertEqual((names.row_count, names.row_length), (804, 16))
        name_row = next(row for row in names.rows if row.row_id == 59)
        self.assertEqual(struct.unpack("<4I", name_row.data), (84, 84, 84, 435))

        labels = specdb.parse_labels((REAL_SPECDB / "asset-02.bin").read_bytes())
        self.assertEqual(labels.table_id, 39)
        self.assertEqual(len(labels), 804)
        self.assertEqual(next(label for label in labels if label.name == "ka_01"),
                         specdb.Label("ka_01", 59))

        strings = specdb.parse_strings((REAL_SPECDB / "asset-00.bin").read_bytes())
        self.assertEqual(len(strings), 3326)
        self.assertEqual(strings[84], "Ford Ka `01")

        for table in (generic, names):
            written = specdb.write_table(table)
            reloaded = specdb.parse_table(written)
            self.assertFalse(reloaded.compressed)
            self.assertEqual(
                [(row.row_id, row.data) for row in reloaded.rows],
                [(row.row_id, row.data) for row in table.rows],
            )

    def test_course_tables(self):
        names = specdb.parse_table((REAL_SPECDB / "asset-03.bin").read_bytes())
        self.assertEqual((names.row_count, names.row_length), (23, 4))
        course = specdb.parse_table((REAL_SPECDB / "asset-05.bin").read_bytes())
        self.assertEqual((course.row_count, course.row_length), (110, 22))
        for name, table_id, count in (
            ("asset-04.bin", 40, 23),
            ("asset-06.bin", 34, 110),
        ):
            labels = specdb.parse_labels((REAL_SPECDB / name).read_bytes())
            self.assertEqual((labels.table_id, len(labels)), (table_id, count))


if __name__ == "__main__":
    unittest.main()


ISO = Path(os.environ.get("GT4_ISO", ""))


@unittest.skipUnless(ISO.is_file(), "set GT4_ISO to the pinned USA v2.00 image")
class EveryTableTests(unittest.TestCase):
    def test_all_79_shipped_tables_parse_and_round_trip(self):
        import gt4_volume

        with gt4_volume.verified_volume(ISO) as stream:
            catalogue = gt4_volume.read_catalogue(stream)
            paths = sorted(path for path in catalogue.files
                           if path.startswith("specdb/GT4_US2560/")
                           and path.endswith(".dbt"))
            self.assertEqual(len(paths), 79)
            for path in paths:
                record = catalogue.files[path]
                raw = gt4_volume.read_at(
                    stream, gt4_volume.VOLUME_OFFSET + record.offset, record.stored_size,
                    gt4_volume.VOLUME_OFFSET + gt4_volume.VOLUME_SIZE)
                data = gt4_volume.decode_payload(raw, record.kind, record.size)
                table = specdb.parse_table(data)
                again = specdb.parse_table(specdb.write_table(table))
                self.assertEqual([row.data for row in table.rows],
                                 [row.data for row in again.rows], path)
                self.assertEqual([row.row_id for row in table.rows],
                                 [row.row_id for row in again.rows], path)
