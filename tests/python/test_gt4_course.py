"""Controls for the course container reader and writer (TASK-218)."""

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


def synthetic_container(sections: list[tuple[int, bytes]]) -> bytes:
    """Build a container whose sections follow the header in the given slots."""
    table = [0] * course.SLOT_COUNT
    table[0] = 0
    table[1] = course.FIRST_SECTION_OFFSET
    offset = course.FIRST_SECTION_OFFSET
    body = bytearray()
    for slot, data in sections:
        padded = data + bytes((-len(data)) % course.ALIGNMENT)
        table[slot] = offset
        body += padded
        offset += len(padded)
    header = struct.pack("<%dI" % course.SLOT_COUNT, *table)
    return header + bytes(body)


class ContainerTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        data = synthetic_container([(1, b"MDLS" + bytes(12)), (3, b"CRSS" + bytes(28))])
        container = course.read_container(data)
        self.assertEqual([section.slot for section in container.sections], [1, 3])
        self.assertEqual([section.tag for section in container.sections], ["MDLS", "CRSS"])
        self.assertEqual(course.write_container(container), data)
        self.assertEqual(container.size, len(data))

    def test_padding_belongs_to_the_previous_section(self) -> None:
        data = synthetic_container([(1, b"MDLS" + bytes(4))])
        container = course.read_container(data)
        self.assertEqual(container.sections[0].data,
                         b"MDLS" + bytes(4) + bytes(course.ALIGNMENT - 8))

    def test_slot_one_must_be_the_header_size(self) -> None:
        data = bytearray(synthetic_container([(1, b"MDLS" + bytes(12))]))
        struct.pack_into("<I", data, 4, 0x80)
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
            course.read_container(bytes(course.HEADER_SIZE - 4))

    def test_write_refuses_a_moved_section(self) -> None:
        data = synthetic_container([(1, b"MDLS" + bytes(12))])
        container = course.read_container(data)
        moved = course.Section(slot=1, offset=course.FIRST_SECTION_OFFSET + course.ALIGNMENT,
                               data=container.sections[0].data)
        with self.assertRaises(course.CourseError):
            course.write_container(course.CourseContainer(slots=container.slots,
                                                          sections=(moved,)))


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
                record = catalogue.files[path]
                raw = gt4_volume.read_at(
                    stream, gt4_volume.VOLUME_OFFSET + record.offset, record.stored_size,
                    gt4_volume.VOLUME_OFFSET + gt4_volume.VOLUME_SIZE)
                data = gt4_volume.decode_payload(raw, record.kind, record.size)
                container = course.read_container(data)
                self.assertEqual(course.write_container(container), data, path)
                self.assertIsNotNone(container.section_with_tag("CRSS"), path)

    def test_settings_header(self) -> None:
        with gt4_volume.verified_volume(ISO) as stream:
            catalogue = gt4_volume.read_catalogue(stream)
            record = catalogue.files["crs/apricot"]
            raw = gt4_volume.read_at(
                stream, gt4_volume.VOLUME_OFFSET + record.offset, record.stored_size,
                gt4_volume.VOLUME_OFFSET + gt4_volume.VOLUME_SIZE)
            container = course.read_container(
                gt4_volume.decode_payload(raw, record.kind, record.size))
            settings = course.read_settings(container.section_with_tag("CRSS"))
        self.assertEqual(settings.version, 1)
        self.assertEqual(len(settings.content), 0x3F0)
        self.assertEqual(settings.sub_block_offsets, (0x28, 0x2C, 0x30, 0x3C0))


if __name__ == "__main__":
    unittest.main()
