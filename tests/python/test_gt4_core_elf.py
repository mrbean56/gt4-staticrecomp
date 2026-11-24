"""Controls for the retail CORE.GT4 to ELF reconstruction (TASK-219 groundwork)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import struct
import sys
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import gt4_core_elf as core_elf

DISC = Path(os.environ.get("GT4_DISC", ""))


def container(entry: int, records: list[tuple[int, bytes]]) -> bytes:
    payload = struct.pack("<H", 128) + b"\x00" * 128
    payload += struct.pack("<H", 128) + b"\x00" * 128
    payload += struct.pack("<iI", len(records), entry)
    for address, body in records:
        payload += struct.pack("<II", address, len(body)) + body
    packed = zlib.compressobj(9, zlib.DEFLATED, -15)
    stream = packed.compress(payload) + packed.flush()
    return struct.pack("<HI", core_elf.CONTAINER_TAG, len(payload)) + stream


class ContainerTests(unittest.TestCase):
    def test_inflate_returns_the_declared_payload(self):
        core = container(core_elf.ENTRY_POINT, [(0x100000, b"\x00")])
        payload = core_elf.inflate_core(core)
        self.assertEqual(len(payload), struct.unpack_from("<I", core, 2)[0])
        self.assertEqual(struct.unpack_from("<H", payload, 0)[0], 128)

    def test_inflate_rejects_a_bad_tag(self):
        core = bytearray(container(core_elf.ENTRY_POINT, [(0x100000, b"\x00")]))
        core[0:2] = b"\x02\x02"
        with self.assertRaises(core_elf.CoreError):
            core_elf.inflate_core(bytes(core))

    def test_inflate_rejects_a_truncated_stream(self):
        core = container(core_elf.ENTRY_POINT, [(0x100000, b"\x00" * 64)])
        with self.assertRaises(core_elf.CoreError):
            core_elf.inflate_core(core[:-4])

    def test_reconstruct_rejects_another_entry_point(self):
        core = container(0x100000, [(0x100000, b"\x00" * core_elf.TEXT_SIZE),
                                    (0x617A80, b"\x00" * 64)])
        with self.assertRaises(core_elf.CoreError):
            core_elf.reconstruct(core)

    def test_reconstruct_rejects_another_text_size(self):
        core = container(core_elf.ENTRY_POINT, [(0x100000, b"\x00" * 16),
                                               (0x617A80, b"\x00" * 64)])
        with self.assertRaises(core_elf.CoreError):
            core_elf.reconstruct(core)

    def test_reconstruct_rejects_foreign_text(self):
        core = container(core_elf.ENTRY_POINT, [(0x100000, b"\x11" * core_elf.TEXT_SIZE),
                                               (0x617A80, b"\x00" * 64)])
        with self.assertRaises(core_elf.CoreError):
            core_elf.reconstruct(core)


@unittest.skipUnless((DISC / "CORE.GT4").is_file(),
                     "set GT4_DISC to an extracted USA v2.00 disc folder")
class RealDiscTests(unittest.TestCase):
    def test_reconstruction_matches_the_pinned_text(self):
        elf = core_elf.reconstruct((DISC / "CORE.GT4").read_bytes())
        self.assertEqual(len(elf), 6_124_412)
        self.assertEqual(hashlib.sha1(elf[0x1000:0x1000 + core_elf.TEXT_SIZE]).hexdigest(),
                         core_elf.TEXT_SHA1)


if __name__ == "__main__":
    unittest.main()
