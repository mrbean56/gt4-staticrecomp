"""VIF upload inventory of a serialized model packet (TASK-217, TASK-218).

A serialized model (`MDLS`) lists its shapes, and each shape descriptor points at a VIF command
stream that frames exactly to its declared quadword count. The uploads inside carry the model's
vertex data in two families:

- float: 3x32-bit vectors at VU address 0xC000 (positions), 2x32-bit pairs at 0xC040 (texture
  coordinates), 3x32-bit vectors at 0xC080 (unit normals) and a single zero vector at 0xC0C0;
- quantized: 3x16-bit at 0x8000, 2x16-bit at 0x8040, 4x8-bit at 0xC080 and 3x8-bit at 0x8080.

This module frames a packet and decodes the float family into numbers. The quantized family is
reported as address, width and count only: the scale and bias that turn its integers into metres
live in the microcode and are not qualified. Observed on car models (`car/menu`, `car/lod`,
`car/open`) and on every course model section.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct

import gt4_vif_framing

FLOAT_POSITION = 0xC000
FLOAT_TEXTURE = 0xC040
FLOAT_NORMAL = 0xC080
MAX_VECTORS = 65_536


class ModelStreamError(ValueError):
    """The packet does not frame as a model VIF stream, or it asks for more than this reads."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ModelStreamError(message)


@dataclass(frozen=True)
class Upload:
    """One `UNPACK` of a packet: where it writes, how many vectors, and their values."""

    address: int
    components: int
    bits: int
    count: int
    values: tuple[tuple[float, ...], ...] | None

    @property
    def decoded(self) -> bool:
        """True when `values` holds real numbers rather than a quantized payload."""
        return self.values is not None


def _decode(body: bytes, start: int, components: int, count: int) -> tuple[tuple[float, ...], ...]:
    return tuple(struct.unpack_from("<%df" % components, body, start + index * 4 * components)
                 for index in range(count))


def read_uploads(packet: bytes) -> tuple[Upload, ...]:
    """Frame one descriptor packet and return its uploads in stream order.

    Float payloads are decoded; every other upload keeps only its shape. A packet that does not
    end exactly on its last command is refused, because the descriptor's quadword count and the
    stream would then disagree and a writer could not reproduce it.
    """
    require(4 <= len(packet) and len(packet) % 16 == 0, "packet is not a whole quadword stream")
    framed = gt4_vif_framing.frame_packet(packet, initial_cycle=(1, 1))
    consumed = framed["commands"][-1]["next_byte_offset"] if framed["commands"] else 0
    require(consumed == len(packet), "packet does not end on its last command")
    uploads = []
    for command in framed["commands"]:
        if command.get("name") != "UNPACK":
            continue
        count = command["source_vectors"]
        require(count <= MAX_VECTORS, "upload count cap exceeded")
        start = command["byte_offset"] + 4
        values = None
        if command["component_bits"] == 32 and not command["mask_enabled"]:
            values = _decode(packet, start, command["components"], count)
        uploads.append(Upload(address=command["immediate_raw"],
                              components=command["components"],
                              bits=command["component_bits"],
                              count=count,
                              values=values))
    return tuple(uploads)


def positions(uploads) -> tuple[tuple[float, ...], ...]:
    """The position vectors of a packet's uploads (3x32-bit at 0xC000)."""
    return tuple(vector for upload in uploads
                 if upload.decoded and upload.address == FLOAT_POSITION
                 and upload.components == 3 for vector in upload.values)


def normals(uploads) -> tuple[tuple[float, ...], ...]:
    """The normal vectors of a packet's uploads (3x32-bit unit vectors at 0xC080)."""
    return tuple(vector for upload in uploads
                 if upload.decoded and upload.address == FLOAT_NORMAL
                 and upload.components == 3 for vector in upload.values)


def texture_coordinates(uploads) -> tuple[tuple[float, ...], ...]:
    """The texture coordinates of a packet's uploads (2x32-bit pairs at 0xC040)."""
    return tuple(pair for upload in uploads
                 if upload.decoded and upload.address == FLOAT_TEXTURE
                 and upload.components == 2 for pair in upload.values)
