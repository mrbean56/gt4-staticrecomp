"""Controls for the serialized model VIF upload reader (TASK-217, TASK-218)."""

from __future__ import annotations

import math
import os
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import gt4_course
import gt4_model_records
import gt4_model_stream as stream
import gt4_volume

ISO = Path(os.environ.get("GT4_ISO", ""))


def unpack(opcode: int, address: int, count: int, payload: bytes) -> bytes:
    """One UNPACK command, its payload, and NOPs up to a whole quadword."""
    command = struct.pack("<HBB", address, count, opcode) + payload
    return command + bytes((-len(command)) % 16)


def load(stream_file, catalogue, path: str) -> bytes:
    entry = catalogue.files[path]
    raw = gt4_volume.read_at(stream_file, gt4_volume.VOLUME_OFFSET + entry.offset,
                             entry.stored_size,
                             gt4_volume.VOLUME_OFFSET + gt4_volume.VOLUME_SIZE)
    return gt4_volume.decode_payload(raw, entry.kind, entry.size)


class SyntheticTests(unittest.TestCase):
    def test_float_positions_are_decoded(self) -> None:
        packet = unpack(0x68, stream.FLOAT_POSITION, 2,
                        struct.pack("<6f", 1.0, 2.0, 3.0, 4.0, 5.0, 6.0))
        uploads = stream.read_uploads(packet)
        self.assertEqual(stream.positions(uploads), ((1.0, 2.0, 3.0), (4.0, 5.0, 6.0)))
        self.assertTrue(uploads[0].decoded)

    def test_quantized_upload_keeps_its_shape_only(self) -> None:
        packet = unpack(0x69, 0x8000, 2, struct.pack("<6h", 1, 2, 3, 4, 5, 6))
        uploads = stream.read_uploads(packet)
        self.assertEqual(uploads[0].count, 2)
        self.assertEqual((uploads[0].components, uploads[0].bits), (3, 16))
        self.assertFalse(uploads[0].decoded)
        self.assertIsNone(uploads[0].values)

    def test_a_stream_that_does_not_end_on_a_command_is_refused(self) -> None:
        packet = unpack(0x68, stream.FLOAT_POSITION, 1, struct.pack("<3f", 1.0, 2.0, 3.0))
        with self.assertRaises(stream.ModelStreamError):
            stream.read_uploads(packet[:12])

    def test_a_partial_quadword_is_refused(self) -> None:
        with self.assertRaises(stream.ModelStreamError):
            stream.read_uploads(bytes(12))


@unittest.skipUnless(ISO.is_file(), "GT4_ISO is not set")
class CarPacketTests(unittest.TestCase):
    def test_every_car_packet_frames_and_its_geometry_is_sane(self) -> None:
        with gt4_volume.verified_volume(ISO) as file:
            catalogue = gt4_volume.read_catalogue(file)
            data = load(file, catalogue, "car/menu/fdeu0006")
        inventory = gt4_model_records.inventory_asset(data)
        base = inventory["main_model_offset"]
        packets = normals = positions = 0
        for shape in inventory["shapes"]:
            for descriptor in shape["descriptors"]:
                offset = descriptor["model_relative_offset"]
                if offset is None:
                    continue
                packet = data[base + offset: base + offset + descriptor["packet_bytes"]]
                uploads = stream.read_uploads(packet)
                packets += 1
                normals += len(stream.normals(uploads))
                positions += len(stream.positions(uploads))
        self.assertEqual(packets, inventory["total_descriptors"])
        self.assertGreater(positions, 1000)
        self.assertGreater(normals, 1000)

    def test_car_normals_are_unit_length(self) -> None:
        with gt4_volume.verified_volume(ISO) as file:
            catalogue = gt4_volume.read_catalogue(file)
            data = load(file, catalogue, "car/menu/fdeu0006")
        inventory = gt4_model_records.inventory_asset(data)
        base = inventory["main_model_offset"]
        checked = 0
        for shape in inventory["shapes"]:
            for descriptor in shape["descriptors"]:
                offset = descriptor["model_relative_offset"]
                if offset is None:
                    continue
                packet = data[base + offset: base + offset + descriptor["packet_bytes"]]
                for normal in stream.normals(stream.read_uploads(packet)):
                    self.assertAlmostEqual(math.dist(normal, (0.0, 0.0, 0.0)), 1.0, places=3)
                    checked += 1
        self.assertGreater(checked, 1000)


@unittest.skipUnless(ISO.is_file(), "GT4_ISO is not set")
class CoursePacketTests(unittest.TestCase):
    def test_course_packets_use_the_quantized_family(self) -> None:
        with gt4_volume.verified_volume(ISO) as file:
            catalogue = gt4_volume.read_catalogue(file)
            data = load(file, catalogue, "crs/apricot")
        container = gt4_course.read_container(data)
        section = container.section_with_tag("MDLS")
        inventory = gt4_model_records.inventory_asset(section.data)
        base = inventory["main_model_offset"]
        checked = quantized = 0
        for shape in inventory["shapes"][:20]:
            for descriptor in shape["descriptors"]:
                offset = descriptor["model_relative_offset"]
                if offset is None:
                    continue
                packet = section.data[base + offset: base + offset + descriptor["packet_bytes"]]
                for upload in stream.read_uploads(packet):
                    checked += 1
                    if not upload.decoded:
                        quantized += 1
        self.assertGreater(checked, 50)
        self.assertGreater(quantized, checked // 2)


if __name__ == "__main__":
    unittest.main()
