"""Controls for the car clone planner and writer (TASK-216 milestone A4)."""

from __future__ import annotations

import os
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import gt4_car_clone as car_clone
import gt4_specdb as specdb

ISO = Path(os.environ.get("GT4_ISO", ""))


class CarOwnedTests(unittest.TestCase):
    def test_own_and_family_labels_belong_to_the_car(self):
        self.assertTrue(car_clone.car_owned("ka_01", "ka_01"))
        self.assertTrue(car_clone.car_owned("ch_ka_01_a", "ka_01"))
        self.assertTrue(car_clone.car_owned("rt_ka_01_m", "ka_01"))

    def test_class_and_event_labels_do_not(self):
        self.assertFalse(car_clone.car_owned("pr_compact_ka_01", "ka_01"))
        self.assertFalse(car_clone.car_owned("gt4r_compact_ka_01", "ka_01"))


@unittest.skipUnless(ISO.is_file(), "set GT4_ISO to the pinned USA v2.00 image")
class RealDiscTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = car_clone.gt4_car_map.read_specdb(ISO)

    def test_plan_covers_every_car_owned_row(self):
        plan = car_clone.plan_clone(self.tables, "ka_01", "xa_99")
        self.assertEqual(len(plan), 41)
        self.assertEqual(sum(len(entries) for entries in plan.values()), 87)
        self.assertEqual(plan["CHASSIS"], [("ch_xa_99_a", 1441, max(
            row.row_id for row in self.tables["CHASSIS"][1].rows) + 1)])

    def test_every_new_label_and_id_is_free(self):
        plan = car_clone.plan_clone(self.tables, "ka_01", "xa_99")
        for name, entries in plan.items():
            labels, table = self.tables[name]
            taken = {label.name for label in labels}
            ids = {row.row_id for row in table.rows}
            top = max(ids)
            for index, (label, source, new_id) in enumerate(entries):
                self.assertNotIn(label, taken, name)
                self.assertNotIn(new_id, ids, name)
                self.assertGreater(new_id, top, name)
                self.assertIn(source, ids, name)

    def test_an_existing_code_is_refused(self):
        with self.assertRaises(car_clone.CloneError):
            car_clone.plan_clone(self.tables, "ka_01", "m3_04")
        with self.assertRaises(car_clone.CloneError):
            car_clone.plan_clone(self.tables, "ka_01", "ka_01")

    def test_a_named_clone_appends_one_string_per_language(self):
        plan = car_clone.plan_clone(self.tables, "ka_01", "xa_98")
        strings = car_clone.read_strings(ISO)
        replacements = car_clone.apply_clone(self.tables, plan, "Ford Ka `01 Clone", strings)
        self.assertEqual(len(replacements), 91)
        for language in car_clone.LANGUAGES:
            table = specdb.parse_strings(
                replacements[car_clone.SPECDB_PREFIX + language + "_StrDB.sdb"])
            self.assertEqual(table[-1], "Ford Ka `01 Clone", language)
            self.assertEqual(len(table), len(strings[language]) + 1, language)
            rows = specdb.parse_table(
                replacements[car_clone.SPECDB_PREFIX + "CAR_NAME_" + language + ".dbt"])
            labels = specdb.parse_labels(
                replacements[car_clone.SPECDB_PREFIX + "CAR_NAME_" + language + ".idi"])
            clone = next(label for label in labels if label.name == "xa_98")
            donor = next(label for label in self.tables["CAR_NAME_" + language][0]
                         if label.name == "ka_01")
            data = next(row.data for row in rows.rows if row.row_id == clone.row_id)
            original = next(row.data for row in self.tables["CAR_NAME_" + language][1].rows
                            if row.row_id == donor.row_id)
            self.assertEqual(struct.unpack_from("<III", data, 0), (len(table) - 1,) * 3, language)
            self.assertEqual(data[12:], original[12:], language)

    def test_the_written_tables_carry_one_extra_linked_row(self):
        plan = car_clone.plan_clone(self.tables, "ka_01", "xa_99")
        replacements = car_clone.apply_clone(self.tables, plan)
        self.assertEqual(len(replacements), 82)
        for name, entries in plan.items():
            labels, table = self.tables[name]
            rows = specdb.parse_table(replacements[car_clone.SPECDB_PREFIX + name + ".dbt"])
            again = specdb.parse_labels(replacements[car_clone.SPECDB_PREFIX + name + ".idi"])
            self.assertEqual(rows.row_count, table.row_count + len(entries), name)
            self.assertEqual(len(again), len(labels) + len(entries), name)
            by_label = {label.name: label.row_id for label in again}
            for label, source, new_id in entries:
                self.assertEqual(by_label[label], new_id, name)
                self.assertEqual(
                    next(row.data for row in rows.rows if row.row_id == new_id),
                    next(row.data for row in table.rows if row.row_id == source), name)


if __name__ == "__main__":
    unittest.main()


IMAGE = Path(os.environ.get("GT4_CLONE_IMAGE", ""))


@unittest.skipUnless(IMAGE.is_file(), "set GT4_CLONE_IMAGE to an image built by this tool")
class RebuiltCloneTests(unittest.TestCase):
    def test_the_built_image_carries_the_cloned_set(self):
        tables = car_clone.gt4_car_map.read_specdb(IMAGE)
        report = car_clone.gt4_car_map.describe(tables, "xa_99")
        self.assertEqual((report["rows"], report["tables"]), (87, 41))
        donor = car_clone.gt4_car_map.car_rows(tables, "ka_01")
        clone = car_clone.gt4_car_map.car_rows(tables, "xa_99")
        for name, hits in clone.items():
            source = {label.replace("xa_99", "ka_01"): data for label, _, data in donor[name]}
            for label, _, data in hits:
                original = source[label.replace("xa_99", "ka_01")]
                if name.startswith("CAR_NAME_"):
                    # A named clone keeps the donor's row but points at its own string.
                    self.assertEqual(data[12:], original[12:], name)
                    language = name[len("CAR_NAME_"):]
                    strings = car_clone.read_strings(IMAGE)[language]
                    self.assertEqual(struct.unpack_from("<I", data, 0)[0], len(strings) - 1, name)
                else:
                    self.assertEqual(data, original, name)
