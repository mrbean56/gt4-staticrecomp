"""Grow GT4.VOL inside its disc image and move the ISO9660 record with it.

A ported circuit is a new catalogue entry, and the shipped volume has no room for
one: its last entry ends 446,424 B before the volume end.  The filesystem still has
20,987,904 B past that end (it declares 2,697,330,688 B while GT4.VOL ends at
2,676,342,784 B), so the volume grows into it.  The 2.6 GB beyond the declared
filesystem belong to the disc's second layer, not to this tail.

The file is described by its directory record in the root directory: one u32 length
in both byte orders.  That field moves; the primary volume descriptor's volume space
size must **not** move.  The runtime locates the disc's second layer from it (a shipped
image logs "layer 1 at 1317040", the volume space in sectors); patching that field
makes the image boot to a black frame ("145 files, layer 1 at 0").  So the volume grows
only inside the filesystem's own free tail, 20,987,904 B past the shipped volume end.

The volume's own catalogue carries no total size, so nothing else changes.  The region
after the new volume end is the source image's untouched tail.
"""

from __future__ import annotations

import struct
from pathlib import Path

import gt4_volume_write as vw

SECTOR_BYTES = 2048
PVD_SECTOR = 16
VOLUME_SPACE_OFFSET = 80
ROOT_RECORD_OFFSET = 156
VOLUME_NAME = b"GT4.VOL;1"
PAGE_BYTES = vw.PAGE_BYTES


class GrowError(ValueError):
    """A disc image that cannot be grown as read."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GrowError(message)


def read_pvd(stream) -> tuple[int, int, int]:
    """Return (volume space in sectors, root directory extent, root directory size)."""
    stream.seek(PVD_SECTOR * SECTOR_BYTES)
    descriptor = stream.read(SECTOR_BYTES)
    require(len(descriptor) == SECTOR_BYTES, "image ended inside the volume descriptor")
    require(descriptor[0] == 1 and descriptor[1:6] == b"CD001",
            "primary volume descriptor is not at sector 16")
    space_le = struct.unpack_from("<I", descriptor, VOLUME_SPACE_OFFSET)[0]
    space_be = struct.unpack_from(">I", descriptor, VOLUME_SPACE_OFFSET + 4)[0]
    require(space_le == space_be, "volume space size byte orders disagree")
    extent_le = struct.unpack_from("<I", descriptor, ROOT_RECORD_OFFSET + 2)[0]
    extent_be = struct.unpack_from(">I", descriptor, ROOT_RECORD_OFFSET + 6)[0]
    size_le = struct.unpack_from("<I", descriptor, ROOT_RECORD_OFFSET + 10)[0]
    size_be = struct.unpack_from(">I", descriptor, ROOT_RECORD_OFFSET + 14)[0]
    require(extent_le == extent_be and size_le == size_be,
            "root directory record byte orders disagree")
    return space_le, extent_le, size_le


def volume_record(stream) -> tuple[int, int]:
    """Return (directory record offset, record length field offset) for GT4.VOL."""
    _, extent, size = read_pvd(stream)
    stream.seek(extent * SECTOR_BYTES)
    directory = stream.read(size)
    require(len(directory) == size, "image ended inside the root directory")
    position = 0
    while position < len(directory):
        length = directory[position]
        if length == 0:
            position = (position // SECTOR_BYTES + 1) * SECTOR_BYTES
            continue
        record = directory[position:position + length]
        require(len(record) == length, "truncated directory record")
        name_length = record[32]
        if record[33:33 + name_length] == VOLUME_NAME:
            return extent * SECTOR_BYTES + position, extent * SECTOR_BYTES + position + 10
        position += length
    raise GrowError("the root directory has no GT4.VOL record")


def record_extent(stream) -> tuple[int, int]:
    """Return the shipped (extent offset, size) of GT4.VOL from its directory record."""
    record, size_field = volume_record(stream)
    stream.seek(record)
    length = stream.read(1)[0]
    raw = stream.read(length - 1)
    record_bytes = bytes([length]) + raw
    extent_le = struct.unpack_from("<I", record_bytes, 2)[0]
    size_le = struct.unpack_from("<I", record_bytes, 10)[0]
    return extent_le * SECTOR_BYTES, size_le


def grow_image(source: Path, destination: Path, growth_bytes: int,
               replacements: dict[str, bytes] | None = None,
               additions: dict[str, bytes] | None = None) -> dict:
    """Copy *source* with GT4.VOL grown by at least *growth_bytes*.

    ``replacements`` and ``additions`` are the volume writer's own arguments, so a
    builder can grow and change the volume in one pass.  Returns the volume report
    fields a caller pins (sizes, entry count, written hash of the volume region).
    """
    require(growth_bytes > 0, "growth must be positive")
    growth_bytes = (growth_bytes + PAGE_BYTES - 1) // PAGE_BYTES * PAGE_BYTES
    image_size = source.stat().st_size
    with source.open("rb") as stream:
        origin, size = record_extent(stream)
        require(origin % SECTOR_BYTES == 0, "GT4.VOL extent is not sector aligned")
        space, _, _ = read_pvd(stream)
        require(origin + size <= image_size, "GT4.VOL runs past the image")
        new_size = size + growth_bytes
        require(origin + new_size <= space * SECTOR_BYTES,
                "the grown volume would leave the filesystem's volume space")
        plan = vw.build_plan(stream, origin, new_size, replacements, additions)
        record, size_field = volume_record(stream)
        require(space * SECTOR_BYTES >= origin + size,
                "the filesystem does not cover the shipped volume")
        require(not destination.exists(), "destination image already exists")
        with destination.open("xb") as output:
            vw.copy_range(stream, output, 0, origin, image_size)
            report = vw.write_plan(stream, output, plan, origin, new_size, tail_zeros=True)
            vw.copy_range(stream, output, origin + new_size, image_size - origin - new_size,
                          image_size)
            output.seek(size_field)
            output.write(struct.pack("<I", new_size) + struct.pack(">I", new_size))
    return {
        "volume_offset": origin,
        "volume_size": new_size,
        "growth": new_size - size,
        "filesystem_sectors": space,
        "entries": len(report.entries),
        "written_sha256": report.written_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Grow GT4.VOL inside its disc image.")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--grow-mb", type=int, required=True)
    parser.add_argument("--add", action="append", default=[], metavar="NEW=EXISTING",
                        help="append a catalogue path holding another path's payload")
    arguments = parser.parse_args(argv)
    with arguments.source.open("rb") as stream:
        catalogue = vw.read_catalogue(stream, vw.VOLUME_OFFSET, vw.VOLUME_SIZE)
        additions = {}
        for item in arguments.add:
            new, _, existing = item.partition("=")
            require(bool(new and existing), "an addition needs NEW=EXISTING")
            record = catalogue.files[existing]
            stored = vw.read_at(stream, vw.VOLUME_OFFSET + record.offset, record.stored_size,
                                vw.VOLUME_OFFSET + vw.VOLUME_SIZE)
            additions[new] = vw.decode_payload(stored, record.kind, record.size)
    report = grow_image(arguments.source, arguments.destination,
                        arguments.grow_mb * 1024 * 1024, None, additions)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
