"""Offline GT4 SpecDB reader and uncompressed writer.

GT4 USA v2.00 ships GTDB rows in three layers: a 256-entry Huffman prefix table,
a code list, and an uncompressed row block followed by per-row streams. A row
stream starts with a symbol count. Count one copies a block row; a larger count
patches a template row: the first symbol carries the template index in its low
six bits, then a byte mask and the replacement bytes follow. The public prose
says version bit 0x01 marks rows that are not compressed; every shipped table
sets that bit and still uses the dictionary, so the reader detects the
dictionary and row block by structure. ``write_table`` emits the uncompressed
form the game accepts: version | 0x01, sequential row offsets and raw rows.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import struct

GTDB_MAGIC = b"GTDB"
GTID_MAGIC = b"GTID"
GTST_MAGIC = b"GTST"
VERSION_UNCOMPRESSED = 0x00000001
HEADER_SIZE = 16
ROW_INFO_SIZE = 8
PREFIX_BYTES = 256 * 2
DICTIONARY_FIXED_SIZE = 8 + PREFIX_BYTES
CODE_DEFINITION_SIZE = 8
BLOCK_HEADER_SIZE = 8
MAX_ROWS = 1_000_000
MAX_ROW_LENGTH = 4096
MAX_CODES = 256
MAX_BLOCK_ROWS = 65_536
MAX_STRINGS = 10_000_000


class SpecDbError(ValueError):
    """Malformed or unsupported SpecDB data."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SpecDbError(message)


def _u16(data: bytes, offset: int, message: str) -> int:
    require(offset + 2 <= len(data), message)
    return struct.unpack_from("<H", data, offset)[0]


@dataclass(frozen=True)
class Row:
    row_id: int
    data: bytes


@dataclass
class Table:
    row_count: int
    row_length: int
    compressed: bool
    rows: list[Row]
    version: int = 0x00010000


@dataclass(frozen=True)
class Code:
    bits: int
    symbol: int
    code: int


@dataclass
class Dictionary:
    prefix: list[tuple[int, int]]
    codes: list[Code]
    by_code: dict[tuple[int, int], int]
    max_bits: int
    shortest_bits: int = 9
    end: int = 0


@dataclass(frozen=True)
class Label:
    name: str
    row_id: int


class LabelTable(list):
    """GTID labels with the table id and header flag attached."""

    def __init__(self, table_id: int, labels: list[Label], flags: int = 0) -> None:
        super().__init__(labels)
        self.table_id = table_id
        self.flags = flags


def _read_dictionary(data: bytes, offset: int) -> Dictionary | None:
    if offset + 8 > len(data):
        return None
    size, count = struct.unpack_from("<II", data, offset)
    if not 0 <= count <= MAX_CODES:
        return None
    if count == 0:
        # An empty table declares the full dictionary size but stores only the header.
        if size != DICTIONARY_FIXED_SIZE:
            return None
        end = offset + 8
    else:
        if size != DICTIONARY_FIXED_SIZE + count * CODE_DEFINITION_SIZE:
            return None
        end = offset + size
    if end > len(data):
        return None
    codes = []
    for index in range(count):
        bits, symbol, _, code = struct.unpack_from(
            "<BBHI",
            data,
            offset + DICTIONARY_FIXED_SIZE + index * CODE_DEFINITION_SIZE,
        )
        if not 1 <= bits <= 32 or code >> bits:
            return None
        codes.append(Code(bits, symbol, code))
    kraft = sum(2.0**-code.bits for code in codes)
    if kraft > 1.0 + 1e-9:
        return None
    prefix = None
    if count:
        prefix = [
            (data[offset + 8 + index * 2], data[offset + 9 + index * 2])
            for index in range(256)
        ]
        by_pair = {(code.bits, code.symbol): code.code for code in codes}
        for index, (symbol, bits) in enumerate(prefix):
            if not bits:
                continue
            if bits > 8:
                prefix = None
                break
            code = by_pair.get((bits, symbol))
            if code is None or (index & ((1 << bits) - 1)) != code:
                prefix = None
                break
    by_code = {(code.bits, code.code): code.symbol for code in codes}
    shortest = min((code.bits for code in codes), default=1) if prefix is None else 9
    return Dictionary(prefix or [], codes, by_code,
                      max((code.bits for code in codes), default=1), shortest, end)


def _read_block(
    data: bytes, offset: int, row_length: int
) -> tuple[list[bytes], int] | None:
    if offset + BLOCK_HEADER_SIZE > len(data):
        return None
    size, count = struct.unpack_from("<II", data, offset)
    if not 0 <= count <= MAX_BLOCK_ROWS:
        return None
    if size != BLOCK_HEADER_SIZE + count * row_length:
        return None
    end = offset + size
    if end > len(data):
        return None
    rows = [
        data[
            offset + BLOCK_HEADER_SIZE + index * row_length :
            offset + BLOCK_HEADER_SIZE + (index + 1) * row_length
        ]
        for index in range(count)
    ]
    return rows, end


def _peek_bits(data: bytes, bit_offset: int, count: int) -> int:
    value = 0
    for bit in range(count):
        position = bit_offset + bit
        if position // 8 < len(data):
            value |= ((data[position // 8] >> (position % 8)) & 1) << bit
    return value


def _decode_symbol(
    data: bytes, bit_offset: int, end_bit: int, dictionary: Dictionary
) -> tuple[int, int]:
    if dictionary.prefix:
        symbol, bits = dictionary.prefix[_peek_bits(data, bit_offset, 8)]
        if bits:
            require(bit_offset + bits <= end_bit, "GTDB truncated Huffman code")
            return symbol, bits
    for length in range(dictionary.shortest_bits, dictionary.max_bits + 1):
        symbol = dictionary.by_code.get(
            (length, _peek_bits(data, bit_offset, length))
        )
        if symbol is not None:
            require(bit_offset + length <= end_bit, "GTDB truncated Huffman code")
            return symbol, length
    raise SpecDbError("GTDB truncated or invalid Huffman code")


def _decode_row(
    data: bytes,
    offset: int,
    end: int,
    row_length: int,
    block_rows: list[bytes],
    dictionary: Dictionary,
) -> bytes:
    count = data[offset]
    require(count >= 1, "GTDB row has no symbols")
    bit_offset = (offset + 1) * 8
    end_bit = end * 8
    symbols = []
    for _ in range(count):
        symbol, bits = _decode_symbol(data, bit_offset, end_bit, dictionary)
        symbols.append(symbol)
        bit_offset += bits
    header = symbols[0]
    kind = header >> 6
    if kind == 0:
        require(count == 1, "GTDB row copy carries trailing symbols")
        require(header < len(block_rows), "GTDB row copy index out of range")
        return block_rows[header]
    require(kind != 3, "GTDB row type 3 is not supported")
    if kind == 1:
        require(len(symbols) == 1 + row_length, "GTDB raw row length mismatch")
        return bytes(symbols[1 : 1 + row_length])
    template = header & 0x3F
    require(template < len(block_rows), "GTDB row template index out of range")
    mask_length = (row_length + 7) // 8
    require(len(symbols) >= 1 + mask_length, "GTDB row patch mask is truncated")
    mask = symbols[1 : 1 + mask_length]
    values = symbols[1 + mask_length :]
    last_bits = row_length - 8 * (mask_length - 1)
    require(mask[-1] >> last_bits == 0, "GTDB row patch mask exceeds row length")
    needed = sum(byte.bit_count() for byte in mask[:-1])
    needed += (mask[-1] & ((1 << last_bits) - 1)).bit_count()
    require(len(values) == needed, "GTDB row patch value count mismatch")
    row = bytearray(block_rows[template])
    value_index = 0
    for index in range(row_length):
        if (mask[index >> 3] >> (index & 7)) & 1:
            row[index] = values[value_index]
            value_index += 1
    return bytes(row)


def _parse_compressed(
    data: bytes,
    infos: list[tuple[int, int]],
    rows_start: int,
    row_length: int,
    block_rows: list[bytes],
    dictionary: Dictionary,
    version: int,
) -> Table:
    stream = data[rows_start:]
    rows = []
    for index, (row_id, offset) in enumerate(infos):
        require(offset < len(stream), "GTDB row offset past end")
        end = infos[index + 1][1] if index + 1 < len(infos) else len(stream)
        require(end >= offset, "GTDB row offsets are not ascending")
        rows.append(
            Row(
                row_id,
                _decode_row(stream, offset, end, row_length, block_rows, dictionary),
            )
        )
    return Table(row_count=len(rows), row_length=row_length, compressed=True,
                 rows=rows, version=version)


def parse_table(data: bytes) -> Table:
    """Parse a GTDB table, compressed or in the uncompressed writer form."""
    require(len(data) >= HEADER_SIZE, "GTDB header is truncated")
    require(data[:4] == GTDB_MAGIC, "GTDB magic mismatch")
    version, row_count, row_length = struct.unpack_from("<III", data, 4)
    require(0 <= row_count <= MAX_ROWS, "GTDB row count out of range")
    require(0 < row_length <= MAX_ROW_LENGTH, "GTDB row length out of range")
    require(
        HEADER_SIZE + row_count * ROW_INFO_SIZE <= len(data),
        "GTDB row info table exceeds file",
    )
    infos = [
        struct.unpack_from("<II", data, HEADER_SIZE + index * ROW_INFO_SIZE)
        for index in range(row_count)
    ]
    rows_start = HEADER_SIZE + row_count * ROW_INFO_SIZE
    dictionary = _read_dictionary(data, rows_start)
    if dictionary is not None:
        block = _read_block(data, dictionary.end, row_length)
        if block is not None:
            block_rows, stream_start = block
            return _parse_compressed(
                data, infos, stream_start, row_length, block_rows,
                dictionary, version,
            )
        require(version & VERSION_UNCOMPRESSED,
                "GTDB compressed table is missing its row block")
    elif not (version & VERSION_UNCOMPRESSED):
        raise SpecDbError("GTDB compressed table is missing its dictionary")
    require(
        rows_start + row_count * row_length == len(data),
        "GTDB uncompressed row data length mismatch",
    )
    rows = []
    for index, (row_id, offset) in enumerate(infos):
        require(offset == index * row_length,
                "GTDB uncompressed row offsets are not sequential")
        start = rows_start + offset
        rows.append(Row(row_id, data[start : start + row_length]))
    return Table(row_count=row_count, row_length=row_length, compressed=False,
                 rows=rows, version=version)


def write_table(table: Table, compressed: bool = False) -> bytes:
    """Write the raw uncompressed form (version | 0x01) and return its bytes."""
    require(not compressed, "GTDB compressed writing is not supported")
    require(table.row_count == len(table.rows), "GTDB row count mismatch")
    require(0 <= table.row_count <= MAX_ROWS, "GTDB row count out of range")
    require(0 < table.row_length <= MAX_ROW_LENGTH, "GTDB row length out of range")
    for row in table.rows:
        require(len(row.data) == table.row_length, "GTDB row length mismatch")
    header = struct.pack(
        "<4sIII", GTDB_MAGIC, table.version | VERSION_UNCOMPRESSED,
        table.row_count, table.row_length,
    )
    if not table.rows:
        # Shipped empty tables carry an empty dictionary header and an empty block header.
        return header + struct.pack("<II", DICTIONARY_FIXED_SIZE, 0) \
            + struct.pack("<II", BLOCK_HEADER_SIZE, 0)
    infos = b"".join(
        struct.pack("<II", row.row_id, index * table.row_length)
        for index, row in enumerate(table.rows)
    )
    return header + infos + b"".join(row.data for row in table.rows)


def parse_labels(data: bytes) -> LabelTable:
    """Parse a GTID label table (names and row ids) plus its table id."""
    require(len(data) >= HEADER_SIZE, "GTID header is truncated")
    require(data[:4] == GTID_MAGIC, "GTID magic mismatch")
    count, flags, table_id = struct.unpack_from("<III", data, 4)
    require(0 <= count <= MAX_ROWS, "GTID key count out of range")
    names_start = HEADER_SIZE + count * 8
    require(names_start <= len(data), "GTID key table exceeds file")
    labels = []
    for index in range(count):
        name_offset, row_id = struct.unpack_from(
            "<II", data, HEADER_SIZE + index * 8
        )
        require(name_offset % 2 == 0, "GTID name offset is not 2-byte aligned")
        position = names_start + name_offset
        length = _u16(data, position, "GTID name length is truncated")
        require(length >= 1, "GTID name is empty")
        require(position + 2 + length <= len(data), "GTID name exceeds file")
        raw = data[position + 2 : position + 2 + length]
        require(raw[-1] == 0, "GTID name is not NUL-terminated")
        labels.append(Label(raw[:-1].decode("latin-1"), row_id))
    return LabelTable(table_id, labels, flags)


def write_labels(labels: LabelTable) -> bytes:
    """Write a GTID label table: keys in the given order, names NUL-terminated and even-aligned."""
    count = len(labels)
    require(0 <= count <= MAX_ROWS, "GTID key count out of range")
    area = bytearray()
    keys = []
    for label in labels:
        keys.append((len(area), label.row_id))
        raw = label.name.encode("latin-1") + b"\x00"
        require(1 <= len(raw) <= MAX_ROW_LENGTH, "GTID name length out of range")
        area += struct.pack("<H", len(raw)) + raw
        if len(area) % 2:
            area += b"\x00"
    header = struct.pack("<4sIII", GTID_MAGIC, count, labels.flags, labels.table_id)
    key_bytes = b"".join(struct.pack("<II", offset, row_id) for offset, row_id in keys)
    return header + key_bytes + bytes(area)


class StringTable(list):
    """GTST strings with the header fields the writer must keep."""

    def __init__(self, strings: list[str], flags: int = 1, reserved: int = 0) -> None:
        super().__init__(strings)
        self.flags = flags
        self.reserved = reserved


def parse_strings(data: bytes) -> StringTable:
    """Parse a GTST string table; offsets are relative to the string area."""
    require(len(data) >= HEADER_SIZE, "GTST header is truncated")
    require(data[:4] == GTST_MAGIC, "GTST magic mismatch")
    count, flags, reserved = struct.unpack_from("<III", data, 4)
    require(count <= MAX_STRINGS, "GTST string count out of range")
    strings_start = HEADER_SIZE + count * 4
    require(strings_start <= len(data), "GTST offset table exceeds file")
    strings = []
    for index in range(count):
        offset = struct.unpack_from("<I", data, HEADER_SIZE + index * 4)[0]
        position = strings_start + offset
        length = _u16(data, position, "GTST string length is truncated")
        require(length >= 1, "GTST string is empty")
        require(position + 2 + length <= len(data), "GTST string exceeds file")
        raw = data[position + 2 : position + 2 + length]
        require(raw[-1] == 0, "GTST string is not NUL-terminated")
        strings.append(raw[:-1].decode("latin-1"))
    return StringTable(strings, flags, reserved)


def write_strings(strings: StringTable) -> bytes:
    """Write a GTST table: offsets relative to the string area, NUL-terminated and even-aligned."""
    count = len(strings)
    require(count <= MAX_STRINGS, "GTST string count out of range")
    area = bytearray()
    offsets = []
    for value in strings:
        offsets.append(len(area))
        raw = value.encode("latin-1") + b"\x00"
        require(1 <= len(raw) <= MAX_ROW_LENGTH, "GTST string length out of range")
        area += struct.pack("<H", len(raw)) + raw
        if len(area) % 2:
            area += b"\x00"
    header = struct.pack("<4sIII", GTST_MAGIC, count,
                         getattr(strings, "flags", 1), getattr(strings, "reserved", 0))
    return header + b"".join(struct.pack("<I", offset) for offset in offsets) + bytes(area)
    return strings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("table", "labels", "strings"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--row", type=int, action="append", default=[],
                        help="row id to dump (table only)")
    args = parser.parse_args(argv)
    data = args.path.read_bytes()
    if args.kind == "table":
        table = parse_table(data)
        print(f"rows={table.row_count} length={table.row_length} "
              f"compressed={table.compressed}")
        for row_id in args.row:
            matches = [row for row in table.rows if row.row_id == row_id]
            if not matches:
                parser.error(f"row {row_id} not found")
            for row in matches:
                print(f"row {row_id}: {row.data.hex()}")
    elif args.kind == "labels":
        labels = parse_labels(data)
        print(f"table={labels.table_id} labels={len(labels)}")
        for label in labels:
            print(f"{label.row_id}\t{label.name}")
    else:
        strings = parse_strings(data)
        print(f"strings={len(strings)}")
        for index, value in enumerate(strings):
            print(f"{index}\t{value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
