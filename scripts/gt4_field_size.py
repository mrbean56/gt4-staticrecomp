"""Raise every six-car race field to a larger grid (data half of TASK-219).

A GT4 race's field size is one byte of its ``RACE`` SpecDB row: ``MaxEntries`` at row
offset 0x3E (PDTools' ``Race`` mapping puts it between ``?`` at 0x3C and ``StartType`` at
0x40; confirmed against the shipped rows, which are 151 bytes each). The shipped data uses
6 for 537 of the 709 races, 1 for 85 (time trials), 2 for 86 (duels and missions) and 4 for
one, so a field of six is data, not a constant in the game code.

The opponent pool (``specdb/GT4_US2560/RACES/<row id>``) is a candidate list, not the grid:
196 shipped six-car races have fewer than six candidates, down to one
(``it_alfa_gta_0001``), so the engine already repeats pool entries to fill the field.
Raising ``MaxEntries`` therefore needs no pool change.

The table is re-serialized in the uncompressed writer form, which is the layout
``scripts/gt4_volume_write.py`` stores when a table changes. Other entries keep their
shipped bytes.

Usage:
  gt4_field_size.py --iso <disc.iso> --output <mod.iso> [--field-size 24]
  gt4_field_size.py --iso <disc.iso> --inspect
"""

from __future__ import annotations

import argparse
import collections
from dataclasses import replace
import json
from pathlib import Path

import gt4_specdb
import gt4_volume
import gt4_volume_write

RACE_TABLE = "specdb/GT4_US2560/RACE.dbt"
RACES_PREFIX = "specdb/GT4_US2560/RACES/"
FIELD_OFFSET = 0x3E
SIX = 6


class FieldSizeError(ValueError):
    """The image or the requested field size is not usable."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise FieldSizeError(message)


def field_sizes(table: gt4_specdb.Table) -> collections.Counter:
    """Return how many rows carry each MaxEntries value."""
    require(table.row_length > FIELD_OFFSET, "RACE rows are too short for MaxEntries")
    return collections.Counter(row.data[FIELD_OFFSET] for row in table.rows)


def raise_fields(table: gt4_specdb.Table, target: int = 24, source: int = SIX
                 ) -> tuple[bytes, list[tuple[int, int, int]]]:
    """Return the re-serialized table and the (row id, old, new) changes."""
    require(1 <= source < target <= 255, "field sizes must satisfy 1 <= source < target <= 255")
    require(table.row_length > FIELD_OFFSET, "RACE rows are too short for MaxEntries")
    rows = []
    changes = []
    for row in table.rows:
        if row.data[FIELD_OFFSET] != source:
            rows.append(row)
            continue
        data = row.data[:FIELD_OFFSET] + bytes([target]) + row.data[FIELD_OFFSET + 1:]
        rows.append(replace(row, data=data))
        changes.append((row.row_id, source, target))
    require(changes, "no RACE row carries a field size of %d" % source)
    rewritten = replace(table, rows=rows)
    return gt4_specdb.write_table(rewritten), changes


def inspect(source: Path) -> dict:
    """Report the field-size histogram and the opponent pools behind it."""
    with gt4_volume.verified_volume(source) as stream:
        catalogue = gt4_volume.read_catalogue(stream)
        table = gt4_specdb.parse_table(_payload(stream, catalogue, RACE_TABLE))
        pools = {
            int(path[len(RACES_PREFIX):][8:], 16): int.from_bytes(
                _payload(stream, catalogue, path)[:4], "little")
            for path in catalogue.files if path.startswith(RACES_PREFIX)
        }
    sizes = field_sizes(table)
    six = [pools.get(row.row_id) for row in table.rows
           if row.data[FIELD_OFFSET] == SIX]
    return {
        "races": table.row_count,
        "row_length": table.row_length,
        "field_sizes": dict(sorted(sizes.items())),
        "six_car_pools": {
            "smaller_than_six": sum(1 for pool in six if pool is not None and pool < SIX),
            "six_to_23": sum(1 for pool in six if pool is not None and SIX <= pool < 24),
            "24_or_more": sum(1 for pool in six if pool is not None and pool >= 24),
        },
    }


def _payload(stream, catalogue, path: str) -> bytes:
    record = catalogue.files[path]
    raw = gt4_volume.read_at(
        stream, gt4_volume.VOLUME_OFFSET + record.offset, record.stored_size,
        gt4_volume.VOLUME_OFFSET + gt4_volume.VOLUME_SIZE)
    return gt4_volume.decode_payload(raw, record.kind, record.size)


def apply_to_iso(source: Path, destination: Path, target: int = 24) -> dict:
    """Copy the pinned image, raising every six-car field to ``target``."""
    require(not destination.exists(), "output already exists")
    with source.open("rb") as stream:
        catalogue = gt4_volume_write.read_catalogue(
            stream, gt4_volume_write.VOLUME_OFFSET, gt4_volume_write.VOLUME_SIZE)
        require(RACE_TABLE in catalogue.files, "RACE table is absent from the volume")
        table = gt4_specdb.parse_table(_payload(stream, catalogue, RACE_TABLE))
        encoded, changes = raise_fields(table, target)
    report = gt4_volume_write.rewrite_iso(source, destination, {RACE_TABLE: encoded})
    replaced = [entry for entry in report.entries if entry.replaced]
    require([entry.path for entry in replaced] == [RACE_TABLE],
            "the rebuilt image replaced more than the RACE table")
    return {
        "written_sha256": report.written_sha256,
        "field_size": target,
        "races_changed": len(changes),
        "table_decoded_bytes": len(encoded),
        "table_stored_bytes": replaced[0].stored_size,
        "table_reused_stored": replaced[0].reused_stored,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--field-size", type=int, default=24)
    parser.add_argument("--inspect", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.inspect:
        print(json.dumps(inspect(arguments.iso), indent=2))
        return 0
    if arguments.output is None:
        parser.error("--output is required unless --inspect is given")
    print(json.dumps(apply_to_iso(arguments.iso, arguments.output, arguments.field_size),
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
