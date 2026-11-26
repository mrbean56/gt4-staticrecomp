"""GT4 course files: containers and race lines (TASK-218).

A course container (`crs/<id>`, and its `.ad` race data) starts with a table of little-endian
file offsets, then the sections those offsets point at, in ascending order and 0x80-aligned.
The table holds 64 entries for a course and 32 for a `.ad`; slot 1 is the table's size and
where the first section starts. Slot 0 is always zero, and a zero slot is absent. Each section
runs to the next slot's offset (the last one to the end of the file), so trailing padding
belongs to its section.

A `.ad` whose first section is `ADLN` carries the course's eight polylines: a 0x30-byte header,
a table of eight offsets, then one 0x40-byte entry header per line (its record count, then
zeros) followed by that many 0x40-byte records. Seventeen `.ad` files (the photo spots, the two
gymkhana spirals and `testcourse_100m`) start with a different, undecoded section, and
`read_lines` refuses them by magic.

`write_container` reproduces a file byte for byte and `write_lines` reproduces a section, so a
changed file differs only where it was changed.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import gt4_model_probe

SLOT_COUNT = 64
RACE_SLOT_COUNT = 32
ALIGNMENT = 0x80
SETTINGS_MAGIC = b"CRSS"
SETTINGS_HEADER_SIZE = 16
SETTINGS_VERSION = 1
RACE_MAGIC = b"ADLN"
RACE_HEADER_SIZE = 0x30
RACE_LINE_COUNT = 8
RACE_ENTRY_SIZE = 0x40
RACE_RECORD_SIZE = 0x40


class CourseError(ValueError):
    """The file, the section or one of the tables does not match the shipped layout."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CourseError(message)


@dataclass(frozen=True)
class Section:
    """One slot's bytes, from its offset to the next section (or the end of the file)."""

    slot: int
    offset: int
    data: bytes

    @property
    def tag(self) -> str:
        """The first four bytes as text; empty sections carry a zero tag."""
        return self.data[:4].decode("latin-1")


@dataclass(frozen=True)
class CourseContainer:
    """A course file: the verbatim slot table and its sections in slot order."""

    slots: tuple[int, ...]
    sections: tuple[Section, ...]

    @property
    def size(self) -> int:
        return 4 * len(self.slots) + sum(len(section.data) for section in self.sections)

    def section(self, slot: int) -> Section | None:
        return next((section for section in self.sections if section.slot == slot), None)

    def section_with_tag(self, tag: str) -> Section | None:
        return next((section for section in self.sections if section.tag == tag), None)

    def model_sections(self) -> tuple[Section, ...]:
        """The `MDLS` sections of a course file, in slot order."""
        return tuple(section for section in self.sections if section.tag == "MDLS")


@dataclass(frozen=True)
class CourseSettings:
    """The `CRSS` header: version, declared content length and the raw sub-block offsets."""

    version: int
    content: bytes
    sub_block_offsets: tuple[int, ...]


@dataclass(frozen=True)
class RaceLine:
    """One polyline: its verbatim 0x40-byte entry header and its records."""

    header: bytes
    records: tuple[bytes, ...]


@dataclass(frozen=True)
class RaceLines:
    """An `ADLN` section: eight polylines and the padding that follows them."""

    lines: tuple[RaceLine, ...]
    padding: bytes


def read_container(data: bytes) -> CourseContainer:
    """Split one course file (or `.ad`) into its slot table and sections."""
    require(len(data) >= 8, "file is shorter than its slot table")
    table_size = struct.unpack_from("<I", data, 4)[0]
    require(table_size in (4 * RACE_SLOT_COUNT, 4 * SLOT_COUNT),
            "slot table is not 32 or 64 entries")
    require(len(data) >= table_size, "file is shorter than its slot table")
    slots = struct.unpack_from("<%dI" % (table_size // 4), data, 0)
    require(slots[0] == 0, "slot 0 is not zero")
    offsets = [offset for offset in slots if offset]
    require(offsets == sorted(offsets), "section offsets are not ascending")
    require(len(offsets) == len(set(offsets)), "two slots share one offset")
    for offset in offsets:
        require(offset % ALIGNMENT == 0, "section offset is not 0x80-aligned")
        require(offset < len(data), "section offset is past the end of the file")
    sections = []
    for index, offset in enumerate(offsets):
        end = offsets[index + 1] if index + 1 < len(offsets) else len(data)
        slot = slots.index(offset)
        sections.append(Section(slot=slot, offset=offset, data=data[offset:end]))
    require(table_size + sum(len(section.data) for section in sections) == len(data),
            "sections do not tile the file")
    return CourseContainer(slots=slots, sections=tuple(sections))


def write_container(container: CourseContainer) -> bytes:
    """Rebuild a course file from its slot table and sections."""
    require(len(container.slots) in (RACE_SLOT_COUNT, SLOT_COUNT),
            "slot table is not 32 or 64 entries")
    require(container.slots[0] == 0, "slot 0 is not zero")
    output = bytearray(4 * len(container.slots))
    struct.pack_into("<%dI" % len(container.slots), output, 0, *container.slots)
    for section in container.sections:
        require(len(output) == section.offset, "section does not start where the slot says")
        output += section.data
    require(len(output) == container.size, "sections do not cover the container")
    return bytes(output)


def probe_models(section: Section) -> dict:
    """Header and table probe of a model section; course geometry uses the car model form.

    All 892 `MDLS` sections across the 110 shipped courses pass the same probe the car models
    pass, so one serialized-model reader serves both pipelines.
    """
    require(section.tag == "MDLS", "section is not a model section")
    return gt4_model_probe.probe_mdls(section.data, 0)


def read_settings(section: Section) -> CourseSettings:
    """Decode the `CRSS` header; the content after it is kept as bytes."""
    data = section.data
    require(len(data) >= SETTINGS_HEADER_SIZE, "settings section is shorter than its header")
    require(data[:4] == SETTINGS_MAGIC, "settings magic mismatch")
    length, version = struct.unpack_from("<II", data, 12)
    require(version == SETTINGS_VERSION, "unknown settings version")
    require(SETTINGS_HEADER_SIZE + length <= len(data), "settings content exceeds its section")
    sub_blocks = struct.unpack_from("<4I", data, 20)
    return CourseSettings(version=version,
                          content=data[SETTINGS_HEADER_SIZE:SETTINGS_HEADER_SIZE + length],
                          sub_block_offsets=sub_blocks)


def record_position(record: bytes) -> tuple[float, float, float]:
    """The (x, z, heading) a record carries.

    Positions are world units shared with the course and its `.ad` lines. The heading is a
    tangent in radians, rotated 90 degrees from the segment direction: it tracks the stored
    points within about 7 degrees at the shipped ~130 m spacing, so it is a road heading, not
    an exact per-point tangent.
    """
    require(len(record) == RACE_RECORD_SIZE, "race record is not 0x40 bytes")
    x, z = struct.unpack_from("<ff", record, 4)
    heading = struct.unpack_from("<f", record, 0x24)[0]
    return x, z, heading


def read_lines(section: bytes) -> RaceLines:
    """Split an `ADLN` section into its eight polylines and its padding."""
    require(len(section) >= RACE_HEADER_SIZE, "race section is shorter than its header")
    require(section[:4] == RACE_MAGIC, "race section is not ADLN")
    reserved_a, reserved_b, length, reserved_c, count, table, reserved_d = \
        struct.unpack_from("<7I", section, 4)
    require(not (reserved_a or reserved_b or reserved_c or reserved_d),
            "race header carries a nonzero reserved word")
    require(count == RACE_LINE_COUNT, "race section does not hold eight lines")
    require(table == RACE_HEADER_SIZE, "race line table is not at 0x30")
    require(not any(section[0x20:RACE_HEADER_SIZE]), "race header padding is not zero")
    offsets = struct.unpack_from("<%dI" % count, section, table)
    lines = []
    position = table + 4 * count
    for offset in offsets:
        require(offset == position, "race line does not follow the previous one")
        header = section[offset:offset + RACE_ENTRY_SIZE]
        require(len(header) == RACE_ENTRY_SIZE, "race line header is truncated")
        start = offset + RACE_ENTRY_SIZE
        end = start + struct.unpack_from("<I", header, 0)[0] * RACE_RECORD_SIZE
        require(end <= len(section), "race line records exceed the section")
        lines.append(RaceLine(header=header,
                              records=tuple(section[at:at + RACE_RECORD_SIZE]
                                            for at in range(start, end, RACE_RECORD_SIZE))))
        position = end
    require(position == length, "race length does not cover its lines")
    return RaceLines(lines=tuple(lines), padding=section[position:])


def write_lines(lines: RaceLines) -> bytes:
    """Rebuild an `ADLN` section from its lines and padding."""
    count = len(lines.lines)
    require(count == RACE_LINE_COUNT, "race section does not hold eight lines")
    offsets = []
    body = bytearray()
    for line in lines.lines:
        require(len(line.header) == RACE_ENTRY_SIZE, "race line header is not 0x40 bytes")
        require(len(line.records) == struct.unpack_from("<I", line.header, 0)[0],
                "race line count does not match its records")
        for record in line.records:
            require(len(record) == RACE_RECORD_SIZE, "race record is not 0x40 bytes")
        offsets.append(RACE_HEADER_SIZE + 4 * count + len(body))
        body += line.header + b"".join(line.records)
    length = RACE_HEADER_SIZE + 4 * count + len(body)
    output = bytearray(RACE_HEADER_SIZE)
    struct.pack_into("<4s7I", output, 0, RACE_MAGIC, 0, 0, length, 0, count,
                     RACE_HEADER_SIZE, 0)
    output += struct.pack("<%dI" % count, *offsets)
    output += body
    output += lines.padding
    return bytes(output)
