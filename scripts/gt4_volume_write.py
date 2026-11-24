"""Byte-identical GT4.VOL rewrite: ToC, stored entries and optional replacements.

Reads the USA v2.00 disc image, verifies ``docs/inputs/usa-v2.00.json``, and
copies the GT4.VOL region to a new file: the bytes before the catalogue, the
ToC and its padding, then every stored entry in volume order, with the
inter-entry gaps copied verbatim. The catalogue pages are written from their
stored bytes, never re-encoded, so a change that needs new page bytes (an entry
whose stored size changes) is refused instead of guessed.

A replacement is decoded payload bytes for one catalogue path. It is re-framed
with the entry's own kind (raw run, or PS2ZIP: magic, size complement, raw
DEFLATE) and must keep the stored size the catalogue records. When re-framing is
not byte-identical to the stored bytes but the payload did not change, the
stored bytes are copied, so an unchanged payload never moves a byte.

Catalogue decoding mirrors ``scripts/gt4_volume.py`` (standard library only
here; that module imports pycdlib). Keep the two decoders in step.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import struct
import zlib

ROOT = Path(__file__).resolve().parents[1]
DISC_MANIFEST = ROOT / "docs/inputs/usa-v2.00.json"
TOC_OFFSET = 17_483_776
VOLUME_OFFSET = 216_840_192
VOLUME_SIZE = 2_459_502_592
PAGE_BYTES = 2048
CATALOGUE_CAP = 2 * 1024 * 1024
FILE_CAP = 16 * 1024 * 1024
PS2ZIP_MAGIC = 0xFFF7EEC5
CHUNK_BYTES = 1024 * 1024


class VolumeWriteError(ValueError):
    """Unsupported or malformed GT4.VOL input, or a rewrite that cannot be exact."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VolumeWriteError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extent(offset: int, length: int, limit: int, minimum: int = 0) -> None:
    require(offset >= minimum and length >= 0 and offset + length <= limit,
            "extent exceeds its container")


def read_at(stream, offset: int, length: int, limit: int) -> bytes:
    extent(offset, length, limit)
    stream.seek(offset)
    data = stream.read(length)
    require(len(data) == length, "short input read")
    return data


def inflate_exact(data: bytes, length: int, cap: int, *, page: bool = False) -> bytes:
    require(0 <= length <= cap, "decoded length exceeds cap")
    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    try:
        decoded = inflater.decompress(data, length + 1)
    except zlib.error as error:
        raise VolumeWriteError("invalid raw DEFLATE") from error
    require(len(decoded) == length, "decoded length mismatch")
    require(not inflater.unconsumed_tail and not inflater.unused_data,
            "trailing or unconsumed DEFLATE bytes")
    # Stored ToC pages use a sync-flush end marker; file runs must finish.
    require(inflater.eof or (page and data.endswith(b"\x00\x00\xff\xff")),
            "incomplete DEFLATE stream")
    return decoded


def decode_payload(data: bytes, kind: int, size: int) -> bytes:
    require(len(data) <= FILE_CAP and 0 <= size <= FILE_CAP, "file cap exceeded")
    require(kind in (1, 2), "unsupported file kind")
    if kind == 1:
        require(len(data) == size, "raw file size mismatch")
        require(data[:4] != struct.pack("<I", PS2ZIP_MAGIC),
                "PS2ZIP under raw record kind is not qualified")
        return data
    require(len(data) >= 8, "truncated PS2ZIP header")
    magic, complement = struct.unpack_from("<II", data)
    require(magic == PS2ZIP_MAGIC, "unsupported compressed file wrapper")
    require((size + complement) & 0xFFFFFFFF == 0, "PS2ZIP size mismatch")
    return inflate_exact(data[8:], size, FILE_CAP)


def frame_payload(decoded: bytes, kind: int) -> bytes:
    """Frame decoded payload bytes the way the entry kind stores them."""
    require(len(decoded) <= FILE_CAP, "file cap exceeded")
    require(kind in (1, 2), "unsupported file kind")
    if kind == 1:
        return decoded
    # Level 9 gives the smallest re-framing, which is the most likely to keep
    # the stored size; an unchanged payload still falls back to its stored bytes.
    encoder = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    compressed = encoder.compress(decoded) + encoder.flush(zlib.Z_FINISH)
    return struct.pack("<II", PS2ZIP_MAGIC, (-len(decoded)) & 0xFFFFFFFF) + compressed


@dataclass(frozen=True)
class Record:
    parent: int
    name: str
    kind: int
    node_id: int = 0
    offset: int = 0
    stored_size: int = 0
    size: int = 0


@dataclass(frozen=True)
class Catalogue:
    files: dict[str, Record]
    directories: int
    pages: int
    records: int
    stored_sha256: str
    decoded_sha256: str


def check_name(name: str, *, root: bool = False) -> None:
    require(bool(name) and not any(char in name for char in "/\\\x00:"),
            "unsafe record name")
    require(name not in (".", "..") or (root and name == "."),
            "relative record name")


def resolve_records(records: list[Record]) -> tuple[dict[str, Record], int]:
    roots = [record for record in records
             if record.parent == 0 and record.name == "." and record.kind == 0]
    require(len(roots) == 1, "expected exactly one root directory")
    root = roots[0]
    directories = {}
    siblings = set()
    for record in records:
        check_name(record.name, root=record is root)
        require(record.parent != 0 or record is root, "extra root record")
        key = (record.parent, record.name)
        require(key not in siblings, "duplicate sibling name")
        siblings.add(key)
        if record.kind == 0:
            require(record.node_id > 0 and record.node_id not in directories,
                    "invalid or duplicate directory ID")
            directories[record.node_id] = record
    paths = {root.node_id: ""}
    for directory_id in directories:
        chain = []
        current = directory_id
        while current not in paths:
            require(current in directories and current not in chain and len(chain) < 64,
                    "missing parent, directory cycle or excessive depth")
            chain.append(current)
            current = directories[current].parent
        for child in reversed(chain):
            node = directories[child]
            paths[child] = (paths[node.parent] + "/" + node.name).lstrip("/")
    files = {}
    for record in records:
        if record is root:
            continue
        require(record.parent in paths, "missing parent directory")
        if record.kind != 0:
            name = (paths[record.parent] + "/" + record.name).lstrip("/")
            files[name] = record
    return files, len(directories)


def parse_catalogue(stored: bytes, volume_size: int) -> Catalogue:
    require(64 <= len(stored) <= CATALOGUE_CAP, "catalogue input cap/header")
    magic, version, stored_size, physical_pages, page_bytes, logical_pages = struct.unpack_from(
        "<IIIIHH", stored)
    require(magic == 0xACB990AD and version == 0x00030001, "unsupported RoFS header")
    require(stored_size == len(stored) and page_bytes == PAGE_BYTES,
            "catalogue size/page size mismatch")
    require(logical_pages > 0 and logical_pages * PAGE_BYTES <= CATALOGUE_CAP,
            "catalogue output cap")
    data_start = TOC_OFFSET + physical_pages * PAGE_BYTES
    require(TOC_OFFSET + len(stored) <= data_start <= volume_size,
            "catalogue/data overlap or data outside volume")
    table_end = 64 + 4 * (logical_pages + 1)
    extent(64, table_end - 64, len(stored))
    offsets = [struct.unpack_from("<I", stored, 64 + index * 4)[0]
               ^ (((index + 1) * 0x14AC327A) & 0xFFFFFFFF)
               for index in range(logical_pages + 1)]
    require(offsets[0] == table_end and offsets[-1] == len(stored), "page offset framing")
    require(all(0 < right - left <= 4096 for left, right in zip(offsets, offsets[1:])),
            "unordered or oversized stored page")
    records = []
    decoded_hash = hashlib.sha256()
    for left, right in zip(offsets, offsets[1:]):
        encrypted = inflate_exact(stored[left:right], PAGE_BYTES, PAGE_BYTES, page=True)
        page = bytes(value ^ 0x55 for value in encrypted)
        decoded_hash.update(page)
        kind, count, next_page, previous_page = struct.unpack_from("<HHii", page)
        require(kind in (0, 1), "unsupported page kind")
        require(all(link == -1 or 0 <= link < logical_pages
                    for link in (next_page, previous_page)), "page link out of range")
        require(count % 2 == kind, "page entry count parity")
        entries = count // 2
        require(entries <= (PAGE_BYTES - 16) // 8, "page table exceeds page")
        table_start = PAGE_BYTES - entries * 8 - 4
        # Delimiter interpretation/routing are deliberately not used for lookup.
        for index in range(entries):
            key_offset, key_size, meta_offset, meta_size = struct.unpack_from(
                "<HHHH", page, PAGE_BYTES - (index + 1) * 8)
            extent(key_offset, key_size, table_start, 12)
            require(key_size >= (1 if kind else 5), "short record key")
            if kind == 1:
                child = meta_offset | meta_size << 16
                require(child < logical_pages, "index child out of range")
                continue
            extent(meta_offset, meta_size, table_start, 12)
            require(meta_size > 0, "empty node metadata")
            require(key_offset + key_size <= meta_offset or meta_offset + meta_size <= key_offset,
                    "key/metadata overlap")
            key = page[key_offset:key_offset + key_size]
            try:
                name = key[4:].decode("utf-8")
            except UnicodeDecodeError as error:
                raise VolumeWriteError("invalid record name encoding") from error
            parent = int.from_bytes(key[:4], "big")
            meta = page[meta_offset:meta_offset + meta_size]
            node_kind = meta[0]
            require(node_kind in (0, 1, 2) and meta_size == (5, 13, 17)[node_kind],
                    "unsupported node metadata")
            if node_kind == 0:
                records.append(Record(parent, name, 0, struct.unpack_from("<I", meta, 1)[0]))
            else:
                data_page, modified, packed = struct.unpack_from("<III", meta, 1)
                size = packed if node_kind == 1 else struct.unpack_from("<I", meta, 13)[0]
                offset = data_start + data_page * PAGE_BYTES
                extent(offset, packed, volume_size, data_start)
                records.append(Record(parent, name, node_kind, offset=offset,
                                      stored_size=packed, size=size))
    files, directories = resolve_records(records)
    return Catalogue(files, directories, logical_pages, len(records), digest(stored),
                     decoded_hash.hexdigest())


def read_catalogue(stream, origin: int = 0, volume_size: int = VOLUME_SIZE) -> Catalogue:
    limit = origin + volume_size
    header = read_at(stream, origin + TOC_OFFSET, 64, limit)
    size = struct.unpack_from("<I", header, 8)[0]
    require(64 <= size <= CATALOGUE_CAP, "catalogue input cap")
    stored = read_at(stream, origin + TOC_OFFSET, size, limit)
    return parse_catalogue(stored, volume_size)


def reframe_replacement(record: Record, decoded: bytes, stored: bytes) -> tuple[bytes, bool]:
    """Return the stored bytes to write and whether the original ones were kept."""
    require(len(decoded) == record.size, "replacement decoded size mismatch")
    framed = frame_payload(decoded, record.kind)
    if framed == stored:
        return framed, False
    # Re-framing is not byte-stable across encoders; an unchanged payload keeps
    # its stored bytes so the rewrite stays byte-identical.
    if decoded == decode_payload(stored, record.kind, record.size):
        return stored, True
    require(len(framed) == record.stored_size, "replacement changes the stored size")
    return framed, False


@dataclass(frozen=True)
class EntryReport:
    path: str
    kind: int
    offset: int
    stored_size: int
    size: int
    replaced: bool
    reused_stored: bool


@dataclass(frozen=True)
class Report:
    volume_size: int
    catalogue_sha256: str
    decoded_catalogue_sha256: str
    source_sha256: str
    written_sha256: str
    identical: bool
    entries: tuple[EntryReport, ...]


def copy_range(stream, output, offset: int, length: int, limit: int,
               source_hash, written_hash) -> None:
    extent(offset, length, limit)
    stream.seek(offset)
    remaining = length
    while remaining:
        data = stream.read(min(remaining, CHUNK_BYTES))
        require(bool(data), "short input read")
        output.write(data)
        source_hash.update(data)
        written_hash.update(data)
        remaining -= len(data)


def rewrite_volume_region(stream, destination: Path, replacements: dict[str, bytes] | None = None,
                          *, origin: int = 0, volume_size: int = VOLUME_SIZE) -> Report:
    """Rewrite one volume region; offsets are relative to ``origin``."""
    limit = origin + volume_size
    replacements = {} if replacements is None else replacements
    catalogue = read_catalogue(stream, origin, volume_size)
    ordered = sorted(catalogue.files.items(), key=lambda item: item[1].offset)
    layout = []
    entries = []
    previous_end = 0
    for path, record in ordered:
        require(record.offset >= previous_end, "entry extents overlap or are out of order")
        if record.offset > previous_end:
            layout.append((previous_end, record.offset - previous_end, None))
        layout.append((record.offset, record.stored_size, path))
        previous_end = record.offset + record.stored_size
        entries.append(EntryReport(path, record.kind, record.offset, record.stored_size,
                                   record.size, path in replacements, False))
    require(previous_end <= volume_size, "entry extent exceeds the volume")
    if previous_end < volume_size:
        layout.append((previous_end, volume_size - previous_end, None))
    require(sum(length for _, length, _ in layout) == volume_size,
            "volume layout does not cover the region exactly")

    framed = {}
    for path, decoded in replacements.items():
        require(path in catalogue.files, "replacement path is absent: " + path)
        require(isinstance(decoded, (bytes, bytearray)), "replacement must be payload bytes")
        record = catalogue.files[path]
        stored = read_at(stream, origin + record.offset, record.stored_size, limit)
        framed[path] = reframe_replacement(record, bytes(decoded), stored)
    entries = [replace(entry, reused_stored=framed[entry.path][1])
               if entry.path in framed else entry for entry in entries]

    source_hash = hashlib.sha256()
    written_hash = hashlib.sha256()
    output = destination.open("xb")
    try:
        with output:
            for offset, length, path in layout:
                if path is not None and path in framed:
                    written = framed[path][0]
                    stored = read_at(stream, origin + offset, length, limit)
                    output.write(written)
                    source_hash.update(stored)
                    written_hash.update(written)
                else:
                    copy_range(stream, output, origin + offset, length, limit,
                               source_hash, written_hash)
    except BaseException:
        # A half-written volume must not survive as if it were complete.
        destination.unlink(missing_ok=True)
        raise
    source_sha256 = source_hash.hexdigest()
    written_sha256 = written_hash.hexdigest()
    return Report(volume_size, catalogue.stored_sha256, catalogue.decoded_sha256,
                  source_sha256, written_sha256, source_sha256 == written_sha256,
                  tuple(entries))


def load_disc_pin(path: Path = DISC_MANIFEST) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    require(manifest.get("schema_version") == 1, "unsupported USA v2.00 pin schema")
    disc = manifest.get("disc")
    require(isinstance(disc, dict), "USA v2.00 pin has no disc identity")
    size, sha256 = disc.get("size_bytes"), disc.get("sha256")
    require(type(size) is int and size > 0, "invalid pinned disc size")
    require(isinstance(sha256, str) and len(sha256) == 64
            and all(char in "0123456789abcdef" for char in sha256),
            "invalid pinned disc digest")
    return {"size_bytes": size, "sha256": sha256}


def verify_disc(stream, pin: dict) -> None:
    stream.seek(0)
    hasher = hashlib.sha256()
    size = 0
    while chunk := stream.read(CHUNK_BYTES):
        hasher.update(chunk)
        size += len(chunk)
    require(size == pin["size_bytes"], "ISO size differs from the pinned USA v2.00 disc")
    require(hasher.hexdigest() == pin["sha256"], "ISO does not match the pinned USA v2.00 disc")


def rewrite_volume(source: Path, destination: Path,
                   replacements: dict[str, bytes] | None = None) -> Report:
    """Verify the pinned disc image, then rewrite the GT4.VOL region it holds."""
    pin = load_disc_pin()
    require(VOLUME_OFFSET + VOLUME_SIZE <= pin["size_bytes"],
            "GT4.VOL extent exceeds the pinned disc")
    require(not destination.exists(), "output already exists")
    with source.open("rb") as stream:
        # The whole-disc pin stands in for the ISO9660 extent check gt4_volume
        # makes through pycdlib.
        verify_disc(stream, pin)
        return rewrite_volume_region(stream, destination, replacements,
                                     origin=VOLUME_OFFSET, volume_size=VOLUME_SIZE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replace", action="append", default=[], metavar="PATH=FILE",
                        help="catalogue path and a file holding its decoded payload")
    arguments = parser.parse_args(argv)
    replacements = {}
    for item in arguments.replace:
        path, separator, filename = item.partition("=")
        if not separator or not path or not filename:
            parser.error("--replace needs CATALOGUE_PATH=PAYLOAD_FILE")
        replacements[path] = Path(filename).read_bytes()
    report = rewrite_volume(arguments.iso, arguments.output, replacements or None)
    print(json.dumps({
        "volume_size": report.volume_size,
        "catalogue_sha256": report.catalogue_sha256,
        "decoded_catalogue_sha256": report.decoded_catalogue_sha256,
        "source_sha256": report.source_sha256,
        "written_sha256": report.written_sha256,
        "identical": report.identical,
        "entries": len(report.entries),
        "replaced": [asdict(entry) for entry in report.entries if entry.replaced],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
