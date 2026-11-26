"""Controls for the course container and race-line readers and writers (TASK-218)."""

from __future__ import annotations

import os
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import gt4_course as course
import gt4_volume

ISO = Path(os.environ.get("GT4_ISO", ""))

RACE_LESS = {
    "crs/gymkhana_spiral_r.ad", "crs/gymkhana_spral.ad",
    "crs/ss_brooklyn.ad", "crs/ss_forum.ad", "crs/ss_fremont.ad", "crs/ss_gion.ad",
    "crs/ss_grandcanyon.ad", "crs/ss_louisburg.ad", "crs/ss_nanzenji.ad", "crs/ss_rialt.ad",
    "crs/ss_sagano.ad", "crs/ss_sanmarco.ad", "crs/ss_shibuya.ad", "crs/ss_times.ad",
    "crs/ss_tsukiji.ad", "crs/ss_tsumago.ad", "crs/testcourse_100m.ad",
}


def synthetic_container(sections: list[tuple[int, bytes]],
                        slot_count: int = course.SLOT_COUNT) -> bytes:
    """Build a container whose sections follow the header in the given slots."""
    table = [0] * slot_count
    table[0] = 0
    table[1] = 4 * slot_count
    offset = table[1]
    body = bytearray()
    for slot, data in sections:
        padded = data + bytes((-len(data)) % course.ALIGNMENT)
        table[slot] = offset
        body += padded
        offset += len(padded)
    header = struct.pack("<%dI" % slot_count, *table)
    return header + bytes(body)


def record(index: int) -> bytes:
    """One synthetic record: a position and a heading, then zeros."""
    data = bytearray(course.RACE_RECORD_SIZE)
    struct.pack_into("<Iff", data, 0, index, float(index), float(-index))
    struct.pack_into("<f", data, 0x24, 0.5 * index)
    return bytes(data)


def synthetic_lines(counts: list[int]) -> bytes:
    """Build an ADLN section by hand, with padding after the last record."""
    table = course.RACE_HEADER_SIZE
    offsets = []
    body = bytearray()
    for count in counts:
        offsets.append(table + 4 * len(counts) + len(body))
        body += struct.pack("<I", count) + bytes(course.RACE_ENTRY_SIZE - 4)
        body += b"".join(record(index) for index in range(count))
    length = table + 4 * len(counts) + len(body)
    output = bytearray(course.RACE_HEADER_SIZE)
    struct.pack_into("<4s7I", output, 0, course.RACE_MAGIC, 0, 0, length, 0, len(counts),
                     table, 0)
    output += struct.pack("<%dI" % len(counts), *offsets)
    output += body
    output += bytes((-len(output)) % course.ALIGNMENT)
    return bytes(output)


def payload(stream, catalogue, path: str) -> bytes:
    entry = catalogue.files[path]
    raw = gt4_volume.read_at(stream, gt4_volume.VOLUME_OFFSET + entry.offset,
                             entry.stored_size,
                             gt4_volume.VOLUME_OFFSET + gt4_volume.VOLUME_SIZE)
    return gt4_volume.decode_payload(raw, entry.kind, entry.size)


class ContainerTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        data = synthetic_container([(1, b"MDLS" + bytes(12)), (3, b"CRSS" + bytes(28))])
        container = course.read_container(data)
        self.assertEqual([section.slot for section in container.sections], [1, 3])
        self.assertEqual([section.tag for section in container.sections], ["MDLS", "CRSS"])
        self.assertEqual(course.write_container(container), data)
        self.assertEqual(container.size, len(data))

    def test_race_container_is_the_same_form_with_32_slots(self) -> None:
        data = synthetic_container([(1, b"ADLN" + bytes(12))], slot_count=course.RACE_SLOT_COUNT)
        container = course.read_container(data)
        self.assertEqual(len(container.slots), course.RACE_SLOT_COUNT)
        self.assertEqual(course.write_container(container), data)

    def test_padding_belongs_to_the_previous_section(self) -> None:
        data = synthetic_container([(1, b"MDLS" + bytes(4))])
        container = course.read_container(data)
        self.assertEqual(container.sections[0].data,
                         b"MDLS" + bytes(4) + bytes(course.ALIGNMENT - 8))

    def test_slot_table_is_32_or_64_entries(self) -> None:
        data = bytearray(synthetic_container([(1, b"MDLS" + bytes(12))]))
        struct.pack_into("<I", data, 4, 0x90)
        with self.assertRaises(course.CourseError):
            course.read_container(bytes(data))

    def test_offsets_must_ascend_and_align(self) -> None:
        swapped = bytearray(synthetic_container(
            [(1, b"MDLS" + bytes(12)), (2, b"Vls0" + bytes(12)),
             (3, b"Tex1" + bytes(12)), (4, b"CRSS" + bytes(12))]))
        struct.pack_into("<I", swapped, 8, 0x280)   # slot 2 after slot 4
        struct.pack_into("<I", swapped, 16, 0x180)  # slot 4 before slot 2
        misaligned = bytearray(synthetic_container(
            [(1, b"MDLS" + bytes(12)), (2, b"Vls0" + bytes(12))]))
        struct.pack_into("<I", misaligned, 8, 0x188)
        for data in (swapped, misaligned):
            with self.assertRaises(course.CourseError):
                course.read_container(bytes(data))

    def test_truncated_container_is_refused(self) -> None:
        with self.assertRaises(course.CourseError):
            course.read_container(bytes(4))

    def test_write_refuses_a_moved_section(self) -> None:
        data = synthetic_container([(1, b"MDLS" + bytes(12))])
        container = course.read_container(data)
        moved = course.Section(slot=1,
                               offset=4 * course.SLOT_COUNT + course.ALIGNMENT,
                               data=container.sections[0].data)
        with self.assertRaises(course.CourseError):
            course.write_container(course.CourseContainer(slots=container.slots,
                                                          sections=(moved,)))


class RaceLineTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        data = synthetic_lines([0, 3, 2, 0, 0, 5, 0, 1])
        lines = course.read_lines(data)
        self.assertEqual([len(line.records) for line in lines.lines], [0, 3, 2, 0, 0, 5, 0, 1])
        self.assertEqual(course.write_lines(lines), data)
        self.assertTrue(lines.padding)

    def test_record_position(self) -> None:
        x, z, heading = course.record_position(record(4))
        self.assertEqual((x, z, heading), (4.0, -4.0, 2.0))

    def test_magic_is_checked(self) -> None:
        data = bytearray(synthetic_lines([1] * 8))
        data[:4] = b"XDLN"
        with self.assertRaises(course.CourseError):
            course.read_lines(bytes(data))

    def test_line_count_must_be_eight(self) -> None:
        data = bytearray(synthetic_lines([1] * 8))
        struct.pack_into("<I", data, 0x14, 7)
        with self.assertRaises(course.CourseError):
            course.read_lines(bytes(data))

    def test_lines_must_follow_each_other(self) -> None:
        data = bytearray(synthetic_lines([1, 1, 1, 1, 1, 1, 1, 1]))
        struct.pack_into("<I", data, course.RACE_HEADER_SIZE + 4, 0x60)
        with self.assertRaises(course.CourseError):
            course.read_lines(bytes(data))

    def test_a_short_section_is_refused(self) -> None:
        with self.assertRaises(course.CourseError):
            course.read_lines(bytes(course.RACE_HEADER_SIZE - 4))


@unittest.skipUnless(ISO.is_file(), "GT4_ISO is not set")
class ShippedCourseTests(unittest.TestCase):
    def test_every_course_round_trips(self) -> None:
        with gt4_volume.verified_volume(ISO) as stream:
            catalogue = gt4_volume.read_catalogue(stream)
            paths = sorted(path for path in catalogue.files
                           if path.startswith("crs/") and not path.endswith(".ad")
                           and "/bestline/" not in path)
            self.assertEqual(len(paths), 110)
            for path in paths:
                data = payload(stream, catalogue, path)
                container = course.read_container(data)
                self.assertEqual(course.write_container(container), data, path)
                self.assertIsNotNone(container.section_with_tag("CRSS"), path)

    def test_settings_header(self) -> None:
        with gt4_volume.verified_volume(ISO) as stream:
            catalogue = gt4_volume.read_catalogue(stream)
            data = payload(stream, catalogue, "crs/apricot")
            container = course.read_container(data)
            settings = course.read_settings(container.section_with_tag("CRSS"))
        self.assertEqual(settings.version, 1)
        self.assertEqual(len(settings.content), 0x3F0)
        self.assertEqual(settings.sub_block_offsets, (0x28, 0x2C, 0x30, 0x3C0))


@unittest.skipUnless(ISO.is_file(), "GT4_ISO is not set")
class ShippedModelTests(unittest.TestCase):
    def test_every_course_model_section_probes(self) -> None:
        sections = models = shapes = 0
        with gt4_volume.verified_volume(ISO) as stream:
            catalogue = gt4_volume.read_catalogue(stream)
            paths = sorted(path for path in catalogue.files
                           if path.startswith("crs/") and not path.endswith(".ad")
                           and "/bestline/" not in path)
            for path in paths:
                container = course.read_container(payload(stream, catalogue, path))
                for section in container.model_sections():
                    info = course.probe_models(section)
                    sections += 1
                    models += info["model_count"]
                    shapes += info["shape_count"]
        self.assertEqual(sections, 892)
        self.assertEqual(models, 67565)
        self.assertEqual(shapes, 74390)


@unittest.skipUnless(ISO.is_file(), "GT4_ISO is not set")
class ShippedRaceTests(unittest.TestCase):
    def test_every_race_data_round_trips(self) -> None:
        seen = set()
        with gt4_volume.verified_volume(ISO) as stream:
            catalogue = gt4_volume.read_catalogue(stream)
            paths = sorted(path for path in catalogue.files if path.endswith(".ad"))
            self.assertEqual(len(paths), 110)
            for path in paths:
                data = payload(stream, catalogue, path)
                container = course.read_container(data)
                self.assertEqual(len(container.slots), course.RACE_SLOT_COUNT, path)
                self.assertEqual(course.write_container(container), data, path)
                section = container.section(1).data
                if path in RACE_LESS:
                    self.assertNotEqual(section[:4], course.RACE_MAGIC, path)
                    with self.assertRaises(course.CourseError, msg=path):
                        course.read_lines(section)
                    continue
                lines = course.read_lines(section)
                self.assertEqual(len(lines.lines), course.RACE_LINE_COUNT, path)
                self.assertEqual(course.write_lines(lines), section, path)
                seen.add(path)
            self.assertEqual(len(seen), 93)

    def test_apricot_lines(self) -> None:
        with gt4_volume.verified_volume(ISO) as stream:
            catalogue = gt4_volume.read_catalogue(stream)
            container = course.read_container(payload(stream, catalogue, "crs/apricot.ad"))
        lines = course.read_lines(container.section(1).data)
        self.assertEqual([len(line.records) for line in lines.lines],
                         [0, 45, 45, 48, 12, 0, 50, 25])
        self.assertEqual(course.record_position(lines.lines[1].records[0]),
                         (43.39813232421875, -260.1523742675781, 4.708763122558594))


if __name__ == "__main__":
    unittest.main()
