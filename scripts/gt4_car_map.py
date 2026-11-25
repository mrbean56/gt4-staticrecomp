"""Show every SpecDB row that belongs to one GT4 car (TASK-216 groundwork).

A GT4 car is not one row: it is a closed set of labels across the `GT4_US2560` tables, and each
label carries the car's own code. `ka_01` (Ford Ka `01) and `m3_04` (BMW M3) both resolve to 95
rows in 44 tables, in the same shape:

    ch_ka_01_a      CHASSIS           the car's own data, one row per letter
    br_ka_01_a/b    BRAKE
    ft_ka_01_a..m   FRONTTIRE
    ka_01           GENERIC_CAR, CAR_NAME_* (9), CAR_VARIATION_* (9), ARCADEINFO_NORMAL

and a smaller group scoped to a class or an event rather than to the car alone:

    pr_compact_ka_01    DEFAULT_PARAM      class default parameters
    df_pt_d_ka_01       DEFAULT_PARTS      default parts
    gt4r_compact_ka_01  ENEMY_CARS         one entry per event that fields the car

The label table order is the shipped grouping (cars in car order, so row ids are not ascending);
the row table itself is what the game binary-searches by row id, so a new row takes an id above
that table's current maximum.

Usage:
  gt4_car_map.py --iso <disc.iso> --car ka_01
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
import re
import struct

import gt4_specdb
import gt4_volume

SPECDB_PREFIX = "specdb/GT4_US2560/"


class CarMapError(ValueError):
    """The image or the requested code is not usable."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CarMapError(message)


def _payload(stream, catalogue, filename: str) -> bytes:
    record = catalogue.files[SPECDB_PREFIX + filename]
    raw = gt4_volume.read_at(
        stream, gt4_volume.VOLUME_OFFSET + record.offset, record.stored_size,
        gt4_volume.VOLUME_OFFSET + gt4_volume.VOLUME_SIZE)
    return gt4_volume.decode_payload(raw, record.kind, record.size)


def read_specdb(iso_path: Path) -> dict[str, tuple[gt4_specdb.LabelTable, gt4_specdb.Table]]:
    """Return every labelled GT4_US2560 table, keyed by table name."""
    tables = {}
    with gt4_volume.rebuilt_volume(iso_path) as stream:
        catalogue = gt4_volume.read_catalogue(stream, pinned=False)
        present = {path[len(SPECDB_PREFIX):]
                   for path in catalogue.files
                   if path.startswith(SPECDB_PREFIX)
                   and "/" not in path[len(SPECDB_PREFIX):]}
        # Every labelled table ships both halves; DISABLED_CAR.dbt has no label table.
        names = {name.rsplit(".", 1)[0] for name in present
                 if name.endswith(".dbt") and name[:-4] + ".idi" in present}
        for name in sorted(names):
            labels = gt4_specdb.parse_labels(_payload(stream, catalogue, name + ".idi"))
            table = gt4_specdb.parse_table(_payload(stream, catalogue, name + ".dbt"))
            tables[name] = (labels, table)
    return tables


LETTER_SUFFIX = re.compile(r"^(?P<family>[a-z]{2})_(?P<code>.+?)_(?P<letter>[a-z])$")


def classify(label: str, code: str) -> str:
    """Group a label by how it refers to the car."""
    if label == code:
        return "own"
    match = LETTER_SUFFIX.match(label)
    if match and match.group("code") == code:
        return "family:" + match.group("family")
    family = label.rsplit(code, 1)[0].rstrip("_")
    return "scoped:" + (family or "-")


def car_rows(tables, code: str) -> dict[str, list[tuple[str, int, bytes]]]:
    """Return every row whose label names this car, grouped by table."""
    found = {}
    for name, (labels, table) in tables.items():
        rows = {row.row_id: row.data for row in table.rows}
        hits = [(label.name, label.row_id, rows.get(label.row_id, b""))
                for label in labels if code in label.name]
        if hits:
            found[name] = hits
    return found


def car_catalogue(iso_path: Path) -> list[dict]:
    """Return every car's code, display name and model code, in label order."""
    with gt4_volume.rebuilt_volume(iso_path) as stream:
        catalogue = gt4_volume.read_catalogue(stream, pinned=False)

        def table(name):
            return gt4_specdb.parse_table(_payload(stream, catalogue, name + ".dbt"))

        def labels(name):
            return gt4_specdb.parse_labels(_payload(stream, catalogue, name + ".idi"))

        strings = gt4_specdb.parse_strings(_payload(stream, catalogue, "american_StrDB.sdb"))
        name_row = {label.name: label.row_id for label in labels("CAR_NAME_american")}
        name_index = {row.row_id: struct.unpack_from("<I", row.data, 0)[0]
                      for row in table("CAR_NAME_american").rows}
        variation = {row.row_id: struct.unpack_from("<I", row.data, 0)[0]
                     for row in table("CAR_VARIATION_american").rows}
        model_index = {row.row_id: struct.unpack_from("<I", row.data, 0)[0]
                       for row in table("VARIATIONamerican").rows}
        cars = []
        for label in labels("GENERIC_CAR"):
            require(label.name in name_row, "no CAR_NAME row for " + label.name)
            require(label.row_id in variation, "no CAR_VARIATION row for " + label.name)
            variation_row = variation[label.row_id]
            require(variation_row in model_index, "no VARIATION row for " + label.name)
            cars.append({
                "code": label.name,
                "name": strings[name_index[name_row[label.name]]],
                "model": strings[model_index[variation_row]],
            })
    return cars


def describe(tables, code: str) -> dict:
    found = car_rows(tables, code)
    groups = collections.Counter()
    for hits in found.values():
        for label, _, _ in hits:
            groups[classify(label, code)] += 1
    return {
        "code": code,
        "rows": sum(len(hits) for hits in found.values()),
        "tables": len(found),
        "groups": dict(sorted(groups.items())),
        "by_table": {name: [label for label, _, _ in hits]
                     for name, hits in sorted(found.items())},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--car")
    parser.add_argument("--catalog", action="store_true",
                        help="list every car's code, name and model code")
    arguments = parser.parse_args(argv)
    if arguments.catalog:
        for car in car_catalogue(arguments.iso):
            print(json.dumps(car))
        return 0
    require(arguments.car is not None, "--car is required unless --catalog is given")
    report = describe(read_specdb(arguments.iso), arguments.car)
    require(report["rows"], "no table carries a row for that code")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
