"""Controls for the per-car SpecDB label map (TASK-216 groundwork)."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import gt4_car_map as car_map

ISO = Path(os.environ.get("GT4_ISO", ""))


class ClassifyTests(unittest.TestCase):
    def test_own_code(self):
        self.assertEqual(car_map.classify("ka_01", "ka_01"), "own")

    def test_family_with_a_letter(self):
        self.assertEqual(car_map.classify("ch_ka_01_a", "ka_01"), "family:ch")
        self.assertEqual(car_map.classify("ft_ka_01_m", "ka_01"), "family:ft")

    def test_a_class_or_event_prefix_is_scoped(self):
        self.assertEqual(car_map.classify("pr_compact_ka_01", "ka_01"), "scoped:pr_compact")
        self.assertEqual(car_map.classify("gt4r_compact_ka_01", "ka_01"), "scoped:gt4r_compact")

    def test_a_partial_code_is_never_the_car_itself(self):
        self.assertNotEqual(car_map.classify("ch_evo_01_a", "evo_0"), "own")
        self.assertTrue(car_map.classify("ch_evo_01_a", "evo_0").startswith("scoped:"))
        self.assertEqual(car_map.classify("ch_evo_01_a", "evo_01"), "family:ch")


@unittest.skipUnless(ISO.is_file(), "set GT4_ISO to the pinned USA v2.00 image")
class RealDiscTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = car_map.read_specdb(ISO)

    def test_both_cars_resolve_to_the_same_shape(self):
        for code in ("ka_01", "m3_04"):
            report = car_map.describe(self.tables, code)
            self.assertEqual(report["rows"], 95, code)
            self.assertEqual(report["tables"], 44, code)
            self.assertEqual(report["groups"]["own"], 20, code)

    def test_a_car_has_one_chassis_and_one_engine(self):
        report = car_map.describe(self.tables, "ka_01")
        self.assertEqual(report["by_table"]["CHASSIS"], ["ch_ka_01_a"])
        self.assertEqual(report["by_table"]["ENGINE"], ["en_ka_01_a"])
        self.assertEqual(len(report["by_table"]["GEAR"]), 4)

    def test_catalogue_names_and_models(self):
        cars = car_map.car_catalogue(ISO)
        self.assertEqual(len(cars), 722)
        ka = next(car for car in cars if car["code"] == "ka_01")
        self.assertEqual(ka, {"code": "ka_01", "name": "Ford Ka `01", "model": "fdeu0006"})
        self.assertEqual(len({car["model"] for car in cars}), 722)
        self.assertTrue(all(car["name"] and car["model"] for car in cars))

    def test_an_unknown_code_has_no_rows(self):
        report = car_map.describe(self.tables, "zz_99")
        self.assertEqual((report["rows"], report["tables"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
