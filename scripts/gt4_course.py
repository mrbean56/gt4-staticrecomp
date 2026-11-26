"""GT4 course containers (`crs/<id>`): the 64-slot table and its sections (TASK-218).

A course container starts with a table of 64 little-endian file offsets, then the sections those
offsets point at, in ascending order and 0x80-aligned. Slot 0 is always zero and slot 1 is always
0x100, the end of the table. A zero slot is absent. Each section runs to the next slot's offset
(the last one to the end of the file), so trailing padding belongs to its section.

Every shipped course carries one `Vls0`/`MDLS` family of sections plus a `CRSS` settings block;
`read_settings` decodes the settings header, whose sub-block offsets are kept raw because their
meaning is unknown. `write_container` reproduces a container byte for byte.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SLOT_COUNT = 64
HEADER_SIZE = SLOT_COUNT * 4
FIRST_SECTION_OFFSET = HEADER_SIZE
ALIGNMENT = 0x80
SETTINGS_MAGIC = b"CRSS"
SETTINGS_HEADER_SIZE = 16
SETTINGS_VERSION = 1


class CourseError(ValueError):
    """The container or one of its sections does not match the shipped layout."""


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
        return HEADER_SIZE + sum(len(section.data) for section in self.sections)

    def section(self, slot: int) -> Section | None:
        return next((section for section in self.sections if section.slot == slot), None)

    def section_with_tag(self, tag: str) -> Section | None:
        return next((section for section in self.sections if section.tag == tag), None)


@dataclass(frozen=True)
class CourseSettings:
    """The `CRSS` header: version, declared content length and the raw sub-block offsets."""

    version: int
    content: bytes
    sub_block_offsets: tuple[int, ...]


def read_container(data: bytes) -> CourseContainer:
    """Split one course file into its slot table and sections."""
    require(len(data) >= HEADER_SIZE, "course container is shorter than its slot table")
    slots = struct.unpack_from("<%dI" % SLOT_COUNT, data, 0)
    require(slots[0] == 0, "slot 0 is not zero")
    require(slots[1] == FIRST_SECTION_OFFSET, "slot 1 is not the end of the table")
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
    require(HEADER_SIZE + sum(len(section.data) for section in sections) == len(data),
            "sections do not tile the file")
    return CourseContainer(slots=slots, sections=tuple(sections))


def write_container(container: CourseContainer) -> bytes:
    """Rebuild a course file from its slot table and sections."""
    output = bytearray(HEADER_SIZE)
    struct.pack_into("<%dI" % SLOT_COUNT, output, 0, *container.slots)
    for section in container.sections:
        require(len(output) == section.offset, "section does not start where the slot says")
        output += section.data
    require(len(output) == container.size, "sections do not cover the container")
    return bytes(output)


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
