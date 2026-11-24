"""Rebuild the retail USA v2.00 game ELF from its CORE.GT4 (TASK-219 groundwork).

The disc keeps the game in ``CORE.GT4``: ``[u16 0x0101][u32 LE inflated size][raw DEFLATE]``,
and the inflated payload is two length-prefixed 128-byte blobs, the entry point, and per record
an address, a size and raw bytes — the retail container, not the beta's encrypted one. The ELF is
that container laid out the way ``scripts/ee_producer.py`` expects (text at file 0x1000 / VA
0x100000, ``.reginfo`` right after it, data page-aligned).

The output is a private build artifact: it is written where the caller asks, never into the
repository, and it is not part of any release.

Usage:
  gt4_core_elf.py --core <CORE.GT4> --output <SCUS_973.28.elf>
  gt4_core_elf.py --iso <disc.iso> --output <SCUS_973.28.elf>
"""

from __future__ import annotations

import argparse
import hashlib
import struct
from pathlib import Path
import zlib

from spec2_beta_elf import build_elf, parse_container

CONTAINER_TAG = 0x0101
INFLATED_CAP = 16 * 1024 * 1024
ENTRY_POINT = 0x100008
TEXT_SIZE = 0x517A14
# docs/DECOMP-IDENTITY.md: the text segment of this game's reconstructed ELF.
TEXT_SHA1 = "28422e7e20a6298224294255355487b9a22cef31"
ISO_PATH = "/CORE.GT4;1"


class CoreError(ValueError):
    """CORE.GT4 is not the expected container."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CoreError(message)


def inflate_core(core: bytes) -> bytes:
    require(len(core) > 6, "CORE.GT4 is truncated")
    tag, size = struct.unpack_from("<HI", core)
    require(tag == CONTAINER_TAG, "unexpected CORE.GT4 tag %04x" % tag)
    require(0 < size <= INFLATED_CAP, "CORE.GT4 inflated size out of range")
    decoder = zlib.decompressobj(-15)
    try:
        payload = decoder.decompress(core[6:], INFLATED_CAP + 1)
    except zlib.error as error:
        raise CoreError("invalid CORE.GT4 deflate stream") from error
    require(len(payload) == size, "CORE.GT4 does not inflate to its declared size")
    require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail,
            "CORE.GT4 deflate stream has trailing data")
    return payload


def reconstruct(core: bytes) -> bytes:
    """Return the reconstructed ELF, refusing anything that is not this game."""
    payload = inflate_core(core)
    _, entry, records = parse_container(payload)
    require(entry == ENTRY_POINT, "unexpected entry point %08x" % entry)
    require(any(record["address"] == 0x100000 and record["size"] == TEXT_SIZE
                for record in records), "this CORE.GT4 is not the pinned USA v2.00 build")
    elf = build_elf(entry, records)
    text = elf[0x1000:0x1000 + TEXT_SIZE]
    require(hashlib.sha1(text).hexdigest() == TEXT_SHA1,
            "reconstructed text does not match the pinned SHA-1")
    return elf


def read_core_from_iso(iso_path: Path) -> bytes:
    import io

    import pycdlib

    iso = pycdlib.PyCdlib()
    try:
        with iso_path.open("rb") as stream:
            iso.open_fp(stream)
            record = iso.get_record(iso_path=ISO_PATH)
            require(record.data_length <= INFLATED_CAP, "CORE.GT4 is implausibly large")
            buffer = io.BytesIO()
            iso.get_file_from_iso_fp(buffer, iso_path=ISO_PATH)
            return buffer.getvalue()
    finally:
        iso.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--core", type=Path, help="an extracted CORE.GT4")
    source.add_argument("--iso", type=Path, help="a disc image, read at /CORE.GT4;1")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    require(not arguments.output.exists(), "output already exists: " + str(arguments.output))
    core = (arguments.core.read_bytes() if arguments.core
            else read_core_from_iso(arguments.iso))
    elf = reconstruct(core)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_bytes(elf)
    print(f"{arguments.output}: {len(elf)} bytes, sha256 {hashlib.sha256(elf).hexdigest()}, "
          f"text sha1 {TEXT_SHA1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
