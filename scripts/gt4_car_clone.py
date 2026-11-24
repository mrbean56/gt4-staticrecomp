"""Clone one GT4 car's SpecDB rows under a new car code (TASK-216 milestone A4).

A GT4 car is a closed label set (see `scripts/gt4_car_map.py`): 20 identity rows plus 62 rows
whose label is `<two-letter family>_<code>_<letter>`. This tool copies the car-owned rows under a
new code, appending each row with an id above its table's maximum and each label at the end of its
label table, then rebuilds the image.

Two deliberate limits:

- The class and event rows (`pr_…`, `pt_…`, `df_…`, `gt4r_…`, `gt4sample_…`) are not cloned by
  default: they belong to a class or an event, not to the car, and a clone that added them would
  silently enter events.
- Strings are reused, so the clone displays its donor's name. That is the point of the first
  experiment: it proves that a car code with its row set becomes a second car in the garage
  without touching the string tables.

Usage:
  gt4_car_clone.py --iso <disc.iso> --car ka_01 --as xa_99 --output <clone.iso>
  gt4_car_clone.py --iso <disc.iso> --car ka_01 --as xa_99 --plan
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import gt4_car_map
import gt4_specdb
import gt4_volume_write

SPECDB_PREFIX = "specdb/GT4_US2560/"
MAX_CODE_LENGTH = 24


class CloneError(ValueError):
    """The clone would collide, or the image is not usable."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CloneError(message)


def car_owned(label: str, code: str) -> bool:
    """Whether a label belongs to the car itself rather than to a class or an event."""
    group = gt4_car_map.classify(label, code)
    return group == "own" or group.startswith("family:")


def plan_clone(tables, code: str, new_code: str,
               include_scoped: bool = False) -> dict[str, list[tuple[str, int, int]]]:
    """Return per table the (new label, source row id, new row id) triples."""
    require(code != new_code, "the new code equals the donor code")
    require(0 < len(new_code) <= MAX_CODE_LENGTH
            and all(char.isalnum() or char == "_" for char in new_code),
            "the new code must be short, alphanumeric and underscore-only")
    found = gt4_car_map.car_rows(tables, code)
    require(found, "no table carries a row for the donor code")
    for name, (labels, _) in tables.items():
        require(all(new_code not in label.name for label in labels),
                "the new code already appears in " + name)
    plan = {}
    for name, hits in sorted(found.items()):
        labels, table = tables[name]
        picked = [hit for hit in hits if include_scoped or car_owned(hit[0], code)]
        if not picked:
            continue
        existing = {label.name for label in labels}
        next_id = max(row.row_id for row in table.rows) + 1
        entries = []
        for label, row_id, _ in picked:
            new_label = label.replace(code, new_code)
            require(new_label != label, "a cloned label did not change")
            require(new_label not in existing, "the cloned label already exists: " + new_label)
            existing.add(new_label)
            entries.append((new_label, row_id, next_id))
            next_id += 1
        plan[name] = entries
    return plan


def apply_clone(tables, plan: dict[str, list[tuple[str, int, int]]]) -> dict[str, bytes]:
    """Return the replacement bytes of every changed row table and label table."""
    replacements = {}
    for name, entries in plan.items():
        labels, table = tables[name]
        rows = sorted((row.row_id, row.data) for row in table.rows)
        for new_label, source_row, new_row in entries:
            require(any(row_id == source_row for row_id, _ in rows),
                    "the source row is absent from " + name)
            rows.append((new_row, dict(rows)[source_row]))
        rows.sort()
        rewritten = gt4_specdb.Table(
            row_count=len(rows), row_length=table.row_length, compressed=True,
            version=table.version,
            rows=[gt4_specdb.Row(row_id, data) for row_id, data in rows])
        replacements[SPECDB_PREFIX + name + ".dbt"] = gt4_specdb.write_table(rewritten)
        new_labels = gt4_specdb.LabelTable(labels.table_id, list(labels), labels.flags)
        new_labels.extend(gt4_specdb.Label(label, row_id) for label, _, row_id in entries)
        replacements[SPECDB_PREFIX + name + ".idi"] = gt4_specdb.write_labels(new_labels)
    return replacements


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--car", required=True)
    parser.add_argument("--as", dest="new_code", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plan", action="store_true",
                        help="print the plan without writing an image")
    parser.add_argument("--include-scoped", action="store_true",
                        help="also clone the class and event rows")
    arguments = parser.parse_args(argv)
    tables = gt4_car_map.read_specdb(arguments.iso)
    plan = plan_clone(tables, arguments.car, arguments.new_code, arguments.include_scoped)
    report = {
        "donor": arguments.car,
        "new_code": arguments.new_code,
        "tables": len(plan),
        "rows": sum(len(entries) for entries in plan.values()),
        "by_table": {name: [label for label, _, _ in entries]
                     for name, entries in plan.items()},
    }
    if arguments.plan:
        print(json.dumps(report, indent=2))
        return 0
    require(arguments.output is not None, "--output is required unless --plan is given")
    require(not arguments.output.exists(), "output already exists")
    replacements = apply_clone(tables, plan)
    written = gt4_volume_write.rewrite_iso(arguments.iso, arguments.output, replacements)
    report["written_sha256"] = written.written_sha256
    report["replaced_entries"] = len(replacements)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
