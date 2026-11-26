"""GT4 per-car CARS records: the row index the menus load for a car code.

A car's label resolves through its `.idi` table to a row id, and the menus then read
`specdb/GT4_US2560/CARS/<row id>` (hex, 16 digits). The shipped record is 8 header bytes
and 16 bytes per row: `u32 row id, u32 table id, u32 letter index, u32 zero`. The Ka's 73
records match its `br_ka_01_a`, `ge_ka_01_b` … rows one for one (2026-11-26, Confirmed
offline; the label -> row id -> record chain is confirmed in game).

`clone_index` rewrites a donor record for a cloned car: every row id becomes the clone's
row id, and the table id and letter stay.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct

import gt4_volume

SPECDB_PREFIX = "specdb/GT4_US2560/"
CARS_PREFIX = SPECDB_PREFIX + "CARS/"
HEADER_BYTES = 8
ENTRY_BYTES = 16
MAX_ENTRIES = 4096


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True)
class IndexEntry:
    row_id: int
    table_id: int
    letter: int


def parse_index(data: bytes) -> list[IndexEntry]:
    """Parse one CARS record and check its count, size and entry layout."""
    require(len(data) >= HEADER_BYTES, "CARS record header is truncated")
    count, reserved = struct.unpack_from("<II", data, 0)
    require(0 <= count <= MAX_ENTRIES, "CARS record count out of range")
    require(len(data) == HEADER_BYTES + count * ENTRY_BYTES,
            "CARS record size does not match its count")
    require(reserved == 0, "CARS record reserved field is not zero")
    entries = []
    seen = set()
    for index in range(count):
        row_id, table_id, letter, trailing = struct.unpack_from(
            "<IIII", data, HEADER_BYTES + index * ENTRY_BYTES)
        require(row_id > 0, "CARS record row id is zero")
        require(table_id > 0, "CARS record table id is zero")
        require(trailing == 0, "CARS record trailing field is not zero")
        key = (table_id, letter)
        require(key not in seen, "CARS record repeats a table letter")
        seen.add(key)
        entries.append(IndexEntry(row_id, table_id, letter))
    return entries


def write_index(entries: list[IndexEntry]) -> bytes:
    """Write a CARS record in the shipped layout."""
    require(len(entries) <= MAX_ENTRIES, "CARS record count out of range")
    seen = set()
    body = bytearray()
    for entry in entries:
        require(entry.row_id > 0 and entry.table_id > 0 and entry.letter >= 0,
                "CARS record entry out of range")
        key = (entry.table_id, entry.letter)
        require(key not in seen, "CARS record repeats a table letter")
        seen.add(key)
        body += struct.pack("<IIII", entry.row_id, entry.table_id, entry.letter, 0)
    return struct.pack("<II", len(entries), 0) + bytes(body)


def read_index(iso_path: Path, row_id: int) -> list[IndexEntry]:
    """Read the shipped CARS record at one car row id."""
    require(0 < row_id < 1 << 64, "CARS row id out of range")
    path = CARS_PREFIX + f"{row_id:016X}"
    with gt4_volume.rebuilt_volume(Path(iso_path)) as stream:
        catalogue = gt4_volume.read_catalogue(stream, pinned=False)
        record = catalogue.files.get(path)
        require(record is not None, "no CARS record at row id " + str(row_id))
        raw = gt4_volume.read_at(
            stream, gt4_volume.VOLUME_OFFSET + record.offset, record.stored_size,
            gt4_volume.VOLUME_OFFSET + gt4_volume.VOLUME_SIZE)
        return parse_index(gt4_volume.decode_payload(raw, record.kind, record.size))


def clone_index(entries: list[IndexEntry], tables, plan,
                new_code: str) -> list[IndexEntry]:
    """Return the donor record with its own rows replaced by the clone's row ids.

    Entries the clone plan does not own (shared default rows) keep the donor row id.
    """
    table_names = {}
    for name, (labels, _) in tables.items():
        table_names[labels.table_id] = (name if labels.table_id not in table_names
                                        else None)
    mapping = {}
    for name, triples in plan.items():
        for new_label, source_row, new_row in triples:
            require(new_code in new_label,
                    "clone plan entry does not name the new code in " + name)
            key = (name, source_row)
            require(key not in mapping, "clone plan repeats a donor row in " + name)
            mapping[key] = new_row
    cloned = []
    for entry in entries:
        name = table_names.get(entry.table_id)
        require(name is not None,
                "CARS record table id is unknown or shared: " + str(entry.table_id))
        cloned.append(IndexEntry(mapping.get((name, entry.row_id), entry.row_id),
                                 entry.table_id, entry.letter))
    return cloned
