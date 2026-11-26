"""GT4.VOL rewrite: re-encoded catalogue, relocated payloads, resized replacements.

Reads the USA v2.00 disc image, verifies ``docs/inputs/usa-v2.00.json``, and
copies it with the GT4.VOL region rebuilt: the opaque prefix, the catalogue
re-encoded from its decoded pages, then every stored entry in its original
order with recomputed page-aligned offsets.

Catalogue decoding and encoding mirror ``scripts/gt4_volume.py`` (standard
library only here; that module imports pycdlib). Keep the two decoders in step.

What the encoder reproduces: the 64-byte header (stored size, allocated pages,
page size, logical pages), the XOR-encoded page offset table, and each page's
header, entry table, keys and metadata. A page's unused space (stale bytes, not
zero-filled) and the four bytes before its entry table are carried verbatim
from the decoded page; their meaning is Unknown, so they are never recomputed.
An entry's node kind and metadata length are preserved, so a replacement changes
its payload and sizes, never how the entry is stored.

A replacement is decoded payload bytes for one catalogue path, of any length up
to ``FILE_CAP``. It is re-framed with the entry's own kind (raw run, or PS2ZIP:
magic, size complement, raw DEFLATE). When the decoded payload is unchanged the
stored bytes are copied, so an unchanged entry never moves a byte. Entries keep
their shipped offset unless a growing neighbour needs the space.

A new catalogue path is inserted too (``additions``): the key is a sibling's
parent id plus the name, a full leaf or index page is packed and split, and the
payload is appended after the last entry.

The volume keeps its shipped extent and page allocation: the catalogue must
still fit the pages it was allocated, and the payload layout must fit the
existing GT4.VOL extent. A change that needs either to grow is refused, not
guessed. Whether the game accepts a rebuilt volume, and a catalogue whose stored
page sizes differ from the shipped ones, is Unknown until tried in game.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import struct
from typing import Sequence
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
TOC_MAGIC = 0xACB990AD
TOC_VERSION = 0x00030001
PAGE_XOR_KEY = 0x14AC327A
PAGE_XOR_BYTE = 0x55
MAX_STORED_PAGE = 4096
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
    toc_page: int = 0
    toc_slot: int = 0


@dataclass(frozen=True)
class TocEntry:
    """One catalogue entry: a key (parent and name) and its metadata bytes."""

    key_offset: int
    key: bytes
    meta_offset: int
    meta_size: int
    meta: bytes = b""  # empty on index pages, whose metadata fields hold a child page


@dataclass(frozen=True)
class TocPage:
    """A decoded catalogue page plus the layout bytes the encoder preserves."""

    kind: int
    next_page: int
    previous_page: int
    entries: tuple[TocEntry, ...]
    trailer: bytes  # four bytes before the entry table; meaning Unknown
    layout: bytes   # the decoded page this structure was read from


@dataclass(frozen=True)
class Catalogue:
    files: dict[str, Record]
    directories: int
    pages: int
    records: int
    stored_sha256: str
    decoded_sha256: str
    physical_pages: int = 0
    data_start: int = 0
    page_structures: tuple[TocPage, ...] = ()
    stored_pages: tuple[bytes, ...] = ()


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


def parse_catalogue_header(stored: bytes) -> tuple[int, int, int]:
    """Validate the RoFS header; return (physical_pages, logical_pages, data_start)."""
    require(64 <= len(stored) <= CATALOGUE_CAP, "catalogue input cap/header")
    magic, version, stored_size, physical_pages, page_bytes, logical_pages = struct.unpack_from(
        "<IIIIHH", stored)
    require(magic == TOC_MAGIC and version == TOC_VERSION, "unsupported RoFS header")
    require(not any(stored[20:64]), "unsupported reserved bytes in the RoFS header")
    require(stored_size == len(stored) and page_bytes == PAGE_BYTES,
            "catalogue size/page size mismatch")
    require(logical_pages > 0 and logical_pages * PAGE_BYTES <= CATALOGUE_CAP,
            "catalogue output cap")
    require(physical_pages > 0, "empty catalogue page allocation")
    data_start = TOC_OFFSET + physical_pages * PAGE_BYTES
    require(TOC_OFFSET + len(stored) <= data_start, "catalogue/data overlap")
    return physical_pages, logical_pages, data_start


def decode_stored_page(stored: bytes) -> bytes:
    encrypted = inflate_exact(stored, PAGE_BYTES, PAGE_BYTES, page=True)
    return bytes(value ^ PAGE_XOR_BYTE for value in encrypted)


def parse_page_image(image: bytes, logical_pages: int) -> TocPage:
    """Validate one decoded catalogue page and return its structure."""
    require(len(image) == PAGE_BYTES, "page size mismatch")
    kind, count, next_page, previous_page = struct.unpack_from("<HHii", image)
    require(kind in (0, 1), "unsupported page kind")
    require(all(link == -1 or 0 <= link < logical_pages
                for link in (next_page, previous_page)), "page link out of range")
    require(count % 2 == kind, "page entry count parity")
    slots = count // 2
    require(slots <= (PAGE_BYTES - 16) // 8, "page table exceeds page")
    table_start = PAGE_BYTES - slots * 8 - 4
    entries = []
    for index in range(slots):
        key_offset, key_size, meta_offset, meta_size = struct.unpack_from(
            "<HHHH", image, PAGE_BYTES - (index + 1) * 8)
        extent(key_offset, key_size, table_start, 12)
        require(key_size >= (1 if kind else 5), "short record key")
        if kind == 1:
            require((meta_offset | meta_size << 16) < logical_pages,
                    "index child out of range")
            entries.append(TocEntry(key_offset, image[key_offset:key_offset + key_size],
                                    meta_offset, meta_size))
            continue
        extent(meta_offset, meta_size, table_start, 12)
        require(meta_size > 0, "empty node metadata")
        require(key_offset + key_size <= meta_offset or meta_offset + meta_size <= key_offset,
                "key/metadata overlap")
        entries.append(TocEntry(key_offset, image[key_offset:key_offset + key_size],
                                meta_offset, meta_size,
                                image[meta_offset:meta_offset + meta_size]))
    return TocPage(kind, next_page, previous_page, tuple(entries),
                   image[table_start:table_start + 4], image)


def stored_page_slices(stored: bytes, logical_pages: int) -> list[bytes]:
    """Return each stored page, checking the XOR-encoded offset framing."""
    table_end = 64 + 4 * (logical_pages + 1)
    extent(64, table_end - 64, len(stored))
    offsets = [struct.unpack_from("<I", stored, 64 + index * 4)[0]
               ^ (((index + 1) * PAGE_XOR_KEY) & 0xFFFFFFFF)
               for index in range(logical_pages + 1)]
    require(offsets[0] == table_end and offsets[-1] == len(stored), "page offset framing")
    require(all(0 < right - left <= MAX_STORED_PAGE
                for left, right in zip(offsets, offsets[1:])),
            "unordered or oversized stored page")
    return [stored[left:right] for left, right in zip(offsets, offsets[1:])]


def parse_page_images(stored: bytes, logical_pages: int) -> tuple[TocPage, ...]:
    """Decode every stored catalogue page."""
    return tuple(parse_page_image(decode_stored_page(slice_), logical_pages)
                 for slice_ in stored_page_slices(stored, logical_pages))


def encode_page(page: TocPage) -> bytes:
    """Rebuild one decoded page image from its structure.

    The unused bytes of the page (stale, not zero-filled) and the four trailer
    bytes keep their decoded values; keys and metadata are written from the
    structure over emptied regions, so no stale byte can stand in for either.
    """
    slots = len(page.entries)
    table_start = PAGE_BYTES - slots * 8 - 4
    image = bytearray(page.layout)
    require(len(image) == PAGE_BYTES and page.kind in (0, 1), "unsupported page kind")
    struct.pack_into("<HHii", image, 0, page.kind, 2 * slots + page.kind,
                     page.next_page, page.previous_page)
    for entry in page.entries:
        extent(entry.key_offset, len(entry.key), table_start, 12)
        image[entry.key_offset:entry.key_offset + len(entry.key)] = bytes(len(entry.key))
        if page.kind == 0:
            require(entry.meta_size == len(entry.meta), "metadata size mismatch")
            extent(entry.meta_offset, len(entry.meta), table_start, 12)
            image[entry.meta_offset:entry.meta_offset + len(entry.meta)] = bytes(len(entry.meta))
    for index, entry in enumerate(page.entries):
        struct.pack_into("<HHHH", image, PAGE_BYTES - (index + 1) * 8,
                         entry.key_offset, len(entry.key),
                         entry.meta_offset, entry.meta_size)
        image[entry.key_offset:entry.key_offset + len(entry.key)] = entry.key
        if page.kind == 0:
            image[entry.meta_offset:entry.meta_offset + len(entry.meta)] = entry.meta
    image[table_start:table_start + 4] = page.trailer
    return bytes(image)


def encode_stored_page(page: TocPage) -> bytes:
    """XOR-encrypt and raw-DEFLATE one page, finished rather than sync-flushed."""
    image = encode_page(page)
    encoder = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    stored = encoder.compress(bytes(value ^ PAGE_XOR_BYTE for value in image))
    stored += encoder.flush(zlib.Z_FINISH)
    require(0 < len(stored) <= MAX_STORED_PAGE, "stored page exceeds its slot")
    return stored


def encode_catalogue(pages: Sequence[TocPage], stored_pages: Sequence[bytes],
                     physical_pages: int) -> bytes:
    """Assemble a stored catalogue: header, XOR offset table, stored pages."""
    require(len(pages) == len(stored_pages) and len(pages) > 0,
            "catalogue page count mismatch")
    require(len(pages) * PAGE_BYTES <= CATALOGUE_CAP, "catalogue output cap")
    table_end = 64 + 4 * (len(pages) + 1)
    offsets = [table_end]
    for stored in stored_pages:
        require(0 < len(stored) <= MAX_STORED_PAGE, "stored page exceeds its slot")
        offsets.append(offsets[-1] + len(stored))
    require(offsets[-1] <= physical_pages * PAGE_BYTES,
            "catalogue does not fit its allocated pages")
    header = struct.pack("<IIIIHH", TOC_MAGIC, TOC_VERSION, offsets[-1],
                         physical_pages, PAGE_BYTES, len(pages)) + bytes(64 - 20)
    table = b"".join(struct.pack("<I", offset ^ (((index + 1) * PAGE_XOR_KEY) & 0xFFFFFFFF))
                     for index, offset in enumerate(offsets))
    return header + table + b"".join(stored_pages)


def reencode_catalogue(stored: bytes) -> bytes:
    """Re-encode every page of a stored catalogue, keeping its page allocation."""
    physical_pages, logical_pages, _ = parse_catalogue_header(stored)
    pages = parse_page_images(stored, logical_pages)
    return encode_catalogue(pages, [encode_stored_page(page) for page in pages],
                            physical_pages)


def parse_catalogue(stored: bytes, volume_size: int) -> Catalogue:
    physical_pages, logical_pages, data_start = parse_catalogue_header(stored)
    require(data_start <= volume_size, "catalogue data outside volume")
    pages = parse_page_images(stored, logical_pages)
    records = []
    decoded_hash = hashlib.sha256()
    for page_index, page in enumerate(pages):
        decoded_hash.update(page.layout)
        if page.kind == 1:
            continue
        for slot, entry in enumerate(page.entries):
            try:
                name = entry.key[4:].decode("utf-8")
            except UnicodeDecodeError as error:
                raise VolumeWriteError("invalid record name encoding") from error
            parent = int.from_bytes(entry.key[:4], "big")
            meta = entry.meta
            node_kind = meta[0]
            require(node_kind in (0, 1, 2) and len(meta) == (5, 13, 17)[node_kind],
                    "unsupported node metadata")
            if node_kind == 0:
                records.append(Record(parent, name, 0, struct.unpack_from("<I", meta, 1)[0],
                                      toc_page=page_index, toc_slot=slot))
            else:
                data_page, _, packed = struct.unpack_from("<III", meta, 1)
                size = packed if node_kind == 1 else struct.unpack_from("<I", meta, 13)[0]
                offset = data_start + data_page * PAGE_BYTES
                extent(offset, packed, volume_size, data_start)
                records.append(Record(parent, name, node_kind, offset=offset,
                                      stored_size=packed, size=size,
                                      toc_page=page_index, toc_slot=slot))
    files, directories = resolve_records(records)
    return Catalogue(files, directories, logical_pages, len(records), digest(stored),
                     decoded_hash.hexdigest(), physical_pages, data_start,
                     pages, tuple(stored_page_slices(stored, logical_pages)))


def read_catalogue(stream, origin: int = 0, volume_size: int = VOLUME_SIZE) -> Catalogue:
    limit = origin + volume_size
    header = read_at(stream, origin + TOC_OFFSET, 64, limit)
    size = struct.unpack_from("<I", header, 8)[0]
    require(64 <= size <= CATALOGUE_CAP, "catalogue input cap")
    stored = read_at(stream, origin + TOC_OFFSET, size, limit)
    return parse_catalogue(stored, volume_size)


def reframe_replacement(record: Record, decoded: bytes,
                        stored: bytes) -> tuple[bytes, bool, int]:
    """Return (stored bytes, whether the shipped bytes were kept, decoded size)."""
    require(len(decoded) <= FILE_CAP, "file cap exceeded")
    if decoded == decode_payload(stored, record.kind, record.size):
        return stored, True, record.size
    return frame_payload(decoded, record.kind), False, len(decoded)


def patch_page_entry(page: TocPage, slot: int, data_page: int,
                     stored_size: int, size: int) -> TocPage:
    """Rewrite one leaf entry's payload location and sizes, keeping its modified field."""
    entry = page.entries[slot]
    meta = entry.meta
    node_kind = meta[0]
    require(node_kind in (1, 2) and len(meta) == (5, 13, 17)[node_kind],
            "unsupported node metadata")
    modified = struct.unpack_from("<I", meta, 5)[0]
    patched = meta[:1] + struct.pack("<III", data_page, modified, stored_size)
    if node_kind == 2:
        patched += struct.pack("<I", size)
    else:
        require(size == stored_size, "raw entry sizes disagree")
    entries = list(page.entries)
    entries[slot] = replace(entry, meta=patched)
    return replace(page, entries=tuple(entries))


def align4(value: int) -> int:
    return (value + 3) & ~3


def page_data_end(page: TocPage) -> int:
    """Return the first byte after every key and metadata block of a page."""
    end = 12
    for entry in page.entries:
        end = max(end, entry.key_offset + len(entry.key))
        if page.kind == 0:
            end = max(end, entry.meta_offset + entry.meta_size)
    return end


def pack_page(page: TocPage, entries: Sequence[TocEntry]) -> TocPage:
    """Lay entries out compactly, each key followed by its metadata (shipped form)."""
    table_start = PAGE_BYTES - len(entries) * 8 - 4
    cursor = 12
    packed = []
    for entry in entries:
        key_offset = cursor
        cursor = align4(cursor + len(entry.key))
        if page.kind == 0:
            require(entry.meta_size == len(entry.meta), "metadata size mismatch")
            meta_offset = cursor
            cursor = align4(cursor + len(entry.meta))
            packed.append(replace(entry, key_offset=key_offset, meta_offset=meta_offset))
        else:
            packed.append(replace(entry, key_offset=key_offset))
    require(cursor <= table_start, "page entries do not fit their slots")
    return replace(page, entries=tuple(packed), layout=bytes(PAGE_BYTES))


def refresh_trailers(pages: list[TocPage], changed: set[int]) -> None:
    """Rebuild each changed page's trailer from the page links.

    A leaf stores its data end; an index page stores the last page of its chain,
    reached from its first child through the ``next_page`` links (the root's
    chain is the index pages, so it stores the last index page).
    """
    for page_id in sorted(changed):
        page = pages[page_id]
        if page.kind == 0:
            trailer = struct.pack("<HH", 0, align4(page_data_end(page)))
        else:
            require(bool(page.entries), "index page has no children")
            last = page.entries[0].meta_offset | (page.entries[0].meta_size << 16)
            seen = {page_id, last}
            while pages[last].next_page >= 0:
                last = pages[last].next_page
                require(last not in seen, "catalogue page chain loops")
                seen.add(last)
            trailer = struct.pack("<HH", last, 0)
        if trailer != page.trailer:
            pages[page_id] = replace(page, trailer=trailer)


def insert_leaf_entry(page: TocPage, slot: int, key: bytes,
                      meta: bytes) -> TocPage | None:
    """Insert one file entry in the page's free space; None when it does not fit."""
    require(page.kind == 0, "file entry needs a leaf page")
    table_start = PAGE_BYTES - (len(page.entries) + 1) * 8 - 4
    key_offset = page_data_end(page)
    meta_offset = align4(key_offset + len(key))
    if meta_offset + len(meta) > table_start:
        return None
    entries = list(page.entries)
    entries.insert(slot, TocEntry(key_offset, key, meta_offset, len(meta), meta))
    return replace(page, entries=tuple(entries))


def split_page(page: TocPage, entries: Sequence[TocEntry]) -> tuple[TocPage, TocPage]:
    """Split a page's entries in half, both halves packed on fresh pages."""
    require(len(entries) >= 4, "page is too small to split")
    half = len(entries) // 2
    return pack_page(page, entries[:half]), pack_page(page, entries[half:])


def find_leaf_slot(pages: Sequence[TocPage], key: bytes) -> tuple[int, int]:
    """Return the leaf and slot where a key belongs, by descending the page tree.

    The tree is read the way the game reads it: at an index page the first entry
    whose key is greater than the wanted key names the child, and a key past every
    entry continues at the page named by the page's trailer.
    """
    page_id = 0
    seen = set()
    while True:
        page = pages[page_id]
        if page.kind == 0:
            for slot, entry in enumerate(page.entries):
                if entry.key >= key:
                    return page_id, slot
            return page_id, len(page.entries)
        require(page_id not in seen, "catalogue page tree loops")
        seen.add(page_id)
        child = None
        for entry in page.entries:
            if entry.key > key:
                child = entry.meta_offset | (entry.meta_size << 16)
                break
        if child is None:
            child = struct.unpack("<HH", page.trailer)[0]
        page_id = child


def entries_fit(page: TocPage, entries: Sequence[TocEntry]) -> bool:
    """Whether a set of entries fits a page packed key-metadata from offset 12."""
    cursor = 12
    for entry in entries:
        cursor = align4(cursor + len(entry.key))
        if page.kind == 0:
            cursor = align4(cursor + entry.meta_size)
    return cursor <= PAGE_BYTES - len(entries) * 8 - 4


def find_parent(pages: Sequence[TocPage], child: int) -> tuple[int, int]:
    for page_id, page in enumerate(pages):
        if page.kind != 1:
            continue
        for slot, entry in enumerate(page.entries):
            if (entry.meta_offset | (entry.meta_size << 16)) == child:
                return page_id, slot
    raise VolumeWriteError("catalogue page has no parent")


def insert_child(pages: list[TocPage], lower: int, first_key: bytes, upper: int,
                 changed: set[int]) -> None:
    """Point a parent at a new upper sibling, splitting the parent when it is full."""
    parent_id, slot = find_parent(pages, lower)
    page = pages[parent_id]
    entries = list(page.entries)
    old = entries[slot]
    entries[slot] = replace(old, key=first_key)
    entries.insert(slot + 1, TocEntry(0, old.key, upper & 0xFFFF, upper >> 16))
    if entries_fit(page, entries):
        pages[parent_id] = pack_page(page, entries)
        changed.add(parent_id)
        return
    lower_page, upper_page = split_page(page, entries)
    new_id = len(pages)
    upper_page = replace(upper_page, previous_page=parent_id, next_page=page.next_page)
    lower_page = replace(lower_page, next_page=new_id)
    pages.append(upper_page)
    pages[parent_id] = lower_page
    changed.update((parent_id, new_id))
    if page.next_page >= 0:
        pages[page.next_page] = replace(pages[page.next_page], previous_page=new_id)
        changed.add(page.next_page)
    require(parent_id != 0, "catalogue root is full")
    # An index entry's key is the first key of the next child, so the upper
    # half's range starts at the key just before its first entry.
    separator = (upper_page.entries[0].key if page.kind == 0
                 else lower_page.entries[-1].key)
    insert_child(pages, parent_id, separator, new_id, changed)


def insert_file_entry(pages: list[TocPage], key: bytes, meta: bytes,
                      changed: set[int]) -> None:
    """Insert one file record, splitting the leaf (and its parents) when full."""
    leaf_id, slot = find_leaf_slot(pages, key)
    page = pages[leaf_id]
    updated = insert_leaf_entry(page, slot, key, meta)
    if updated is not None:
        pages[leaf_id] = updated
        changed.add(leaf_id)
        return
    entries = list(page.entries)
    entries.insert(slot, TocEntry(0, key, 0, len(meta), meta))
    lower, upper = split_page(page, entries)
    new_id = len(pages)
    upper = replace(upper, previous_page=leaf_id, next_page=page.next_page)
    lower = replace(lower, next_page=new_id)
    pages.append(upper)
    pages[leaf_id] = lower
    changed.update((leaf_id, new_id))
    if page.next_page >= 0:
        pages[page.next_page] = replace(pages[page.next_page], previous_page=new_id)
        changed.add(page.next_page)
    insert_child(pages, leaf_id, upper.entries[0].key, new_id, changed)


def new_file_meta(data_page: int, stored_size: int, size: int) -> bytes:
    """Metadata for a new compressed file entry; the modified field means Unknown."""
    return b"\x02" + struct.pack("<IIII", data_page, 0, stored_size, size)


@dataclass(frozen=True)
class EntryReport:
    path: str
    kind: int
    offset: int
    stored_size: int
    size: int
    source_offset: int
    source_stored_size: int
    source_size: int
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
    physical_pages: int
    data_start: int
    entries: tuple[EntryReport, ...]


@dataclass(frozen=True)
class Plan:
    catalogue: Catalogue
    toc: bytes
    entries: tuple[EntryReport, ...]
    payloads: dict[str, bytes]


def directory_parent(catalogue: Catalogue, path: str) -> int:
    """Return the parent directory id shared by a path's siblings."""
    directory, _, name = path.rpartition("/")
    prefix = directory + "/" if directory else ""
    for other, record in catalogue.files.items():
        if other.startswith(prefix) and "/" not in other[len(prefix):] and other != path:
            return record.parent
    raise VolumeWriteError("addition has no sibling to take its directory from: " + path)


def build_plan(stream, origin: int, volume_size: int,
               replacements: dict[str, bytes] | None = None,
               additions: dict[str, bytes] | None = None) -> Plan:
    """Decide every entry's new offset and the catalogue bytes that describe it."""
    catalogue = read_catalogue(stream, origin, volume_size)
    limit = origin + volume_size
    replacements = {} if replacements is None else replacements
    additions = {} if additions is None else additions
    payloads = {}
    for path, decoded in replacements.items():
        require(path in catalogue.files, "replacement path is absent: " + path)
        require(isinstance(decoded, (bytes, bytearray)), "replacement must be payload bytes")
        record = catalogue.files[path]
        stored = read_at(stream, origin + record.offset, record.stored_size, limit)
        payload, reused, size = reframe_replacement(record, bytes(decoded), stored)
        payloads[path] = (payload, reused, size)
    for path, decoded in additions.items():
        require(path not in catalogue.files, "addition path already exists: " + path)
        require(isinstance(decoded, (bytes, bytearray)), "addition must be payload bytes")

    cursor = catalogue.data_start
    entries = []
    patched: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
    source_end = 0
    for path, record in sorted(catalogue.files.items(), key=lambda item: item[1].offset):
        require(record.offset >= source_end, "entry extents overlap or are out of order")
        source_end = record.offset + record.stored_size
        payload, reused, size = payloads.get(path, (None, False, record.size))
        stored_size = record.stored_size if payload is None else len(payload)
        start = max(cursor, record.offset)
        start = (start + PAGE_BYTES - 1) // PAGE_BYTES * PAGE_BYTES
        data_page, aligned = divmod(start - catalogue.data_start, PAGE_BYTES)
        require(aligned == 0 and 0 <= data_page < 1 << 32,
                "entry offset is not a page index")
        if (start, stored_size, size) != (record.offset, record.stored_size, record.size):
            patched[record.toc_page].append((record.toc_slot, data_page, stored_size, size))
        cursor = start + stored_size
        entries.append(EntryReport(path, record.kind, start, stored_size, size,
                                   record.offset, record.stored_size, record.size,
                                   payload is not None, reused))
    added = []
    for path, decoded in additions.items():
        stored = frame_payload(bytes(decoded), 2)
        start = (cursor + PAGE_BYTES - 1) // PAGE_BYTES * PAGE_BYTES
        data_page, aligned = divmod(start - catalogue.data_start, PAGE_BYTES)
        require(aligned == 0 and 0 <= data_page < 1 << 32,
                "addition offset is not a page index")
        cursor = start + len(stored)
        entries.append(EntryReport(path, 2, start, len(stored), len(decoded),
                                   0, 0, 0, False, False))
        payloads[path] = (stored, False, len(decoded))
        added.append((path, data_page, len(stored), len(decoded)))
    require(cursor <= volume_size, "volume layout does not fit the GT4.VOL extent")

    pages = list(catalogue.page_structures)
    for page_index, items in patched.items():
        for slot, data_page, stored_size, size in items:
            pages[page_index] = patch_page_entry(pages[page_index], slot, data_page,
                                                 stored_size, size)
    changed = set(patched)
    for path, data_page, stored_size, size in added:
        parent = directory_parent(catalogue, path)
        key = parent.to_bytes(4, "big") + path.rpartition("/")[2].encode("utf-8")
        insert_file_entry(pages, key, new_file_meta(data_page, stored_size, size), changed)
    require(len(pages) <= 1 << 16, "catalogue page id space exhausted")
    refresh_trailers(pages, changed)
    stored_pages = []
    for index, page in enumerate(pages):
        if index in changed or index >= len(catalogue.stored_pages):
            stored_pages.append(encode_stored_page(page))
        else:
            stored_pages.append(catalogue.stored_pages[index])
    toc = encode_catalogue(pages, stored_pages, catalogue.physical_pages)
    return Plan(catalogue, toc, tuple(entries), {path: item[0] for path, item in payloads.items()})


def copy_range(stream, output, offset: int, length: int, limit: int,
               written=None) -> None:
    extent(offset, length, limit)
    stream.seek(offset)
    remaining = length
    while remaining:
        data = stream.read(min(remaining, CHUNK_BYTES))
        require(bool(data), "short input read")
        output.write(data)
        if written is not None:
            written.update(data)
        remaining -= len(data)


def hash_region(stream, offset: int, length: int, limit: int) -> str:
    extent(offset, length, limit)
    stream.seek(offset)
    hasher = hashlib.sha256()
    remaining = length
    while remaining:
        data = stream.read(min(remaining, CHUNK_BYTES))
        require(bool(data), "short input read")
        hasher.update(data)
        remaining -= len(data)
    return hasher.hexdigest()


def write_plan(stream, output, plan: Plan, origin: int, volume_size: int) -> Report:
    """Write one planned volume region, at ``origin`` in the source."""
    limit = origin + volume_size
    catalogue = plan.catalogue
    written = hashlib.sha256()

    def emit(data: bytes) -> None:
        output.write(data)
        written.update(data)

    copy_range(stream, output, origin, TOC_OFFSET, limit, written)
    emit(plan.toc)
    pad_start = TOC_OFFSET + len(plan.toc)
    copy_range(stream, output, origin + pad_start, catalogue.data_start - pad_start, limit,
               written)
    position = catalogue.data_start
    for entry in plan.entries:
        require(entry.offset >= position, "entry extents overlap or are out of order")
        if entry.offset > position:
            copy_range(stream, output, origin + position, entry.offset - position, limit,
                       written)
        if entry.path in plan.payloads:
            emit(plan.payloads[entry.path])
        else:
            copy_range(stream, output, origin + entry.source_offset, entry.stored_size,
                       limit, written)
        position = entry.offset + entry.stored_size
    if position < volume_size:
        copy_range(stream, output, origin + position, volume_size - position, limit, written)
    source_sha256 = hash_region(stream, origin, volume_size, limit)
    written_sha256 = written.hexdigest()
    return Report(volume_size, catalogue.stored_sha256, catalogue.decoded_sha256,
                  source_sha256, written_sha256, source_sha256 == written_sha256,
                  catalogue.physical_pages, catalogue.data_start, plan.entries)


def rewrite_volume_region(stream, destination: Path, replacements: dict[str, bytes] | None = None,
                          additions: dict[str, bytes] | None = None,
                          *, origin: int = 0, volume_size: int = VOLUME_SIZE) -> Report:
    """Rewrite one volume region into a fresh file; offsets are relative to ``origin``."""
    plan = build_plan(stream, origin, volume_size, replacements, additions)
    output = destination.open("xb")
    try:
        with output:
            report = write_plan(stream, output, plan, origin, volume_size)
    except BaseException:
        # A half-written volume must not survive as if it were complete.
        destination.unlink(missing_ok=True)
        raise
    return report


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
                   replacements: dict[str, bytes] | None = None,
                   additions: dict[str, bytes] | None = None) -> Report:
    """Verify the pinned disc image, then write its GT4.VOL region alone."""
    pin = load_disc_pin()
    require(VOLUME_OFFSET + VOLUME_SIZE <= pin["size_bytes"],
            "GT4.VOL extent exceeds the pinned disc")
    require(not destination.exists(), "output already exists")
    with source.open("rb") as stream:
        verify_disc(stream, pin)
        return rewrite_volume_region(stream, destination, replacements, additions,
                                     origin=VOLUME_OFFSET, volume_size=VOLUME_SIZE)


def rewrite_iso(source: Path, destination: Path,
                replacements: dict[str, bytes] | None = None,
                additions: dict[str, bytes] | None = None) -> Report:
    """Verify the pinned disc image, then copy it with the GT4.VOL region rebuilt."""
    pin = load_disc_pin()
    require(VOLUME_OFFSET + VOLUME_SIZE <= pin["size_bytes"],
            "GT4.VOL extent exceeds the pinned disc")
    require(not destination.exists(), "output already exists")
    plan = None
    with source.open("rb") as stream:
        verify_disc(stream, pin)
        plan = build_plan(stream, VOLUME_OFFSET, VOLUME_SIZE, replacements, additions)
        output = destination.open("xb")
        try:
            with output:
                copy_range(stream, output, 0, VOLUME_OFFSET, pin["size_bytes"])
                report = write_plan(stream, output, plan, VOLUME_OFFSET, VOLUME_SIZE)
                tail = pin["size_bytes"] - VOLUME_OFFSET - VOLUME_SIZE
                copy_range(stream, output, VOLUME_OFFSET + VOLUME_SIZE, tail, pin["size_bytes"])
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replace", action="append", default=[], metavar="PATH=FILE",
                        help="catalogue path and a file holding its decoded payload")
    parser.add_argument("--add", action="append", default=[], metavar="PATH=FILE",
                        help="new catalogue path and the file holding its decoded payload")
    arguments = parser.parse_args(argv)
    def load_pairs(items, option):
        loaded = {}
        for item in items:
            path, separator, filename = item.partition("=")
            if not separator or not path or not filename:
                parser.error(option + " needs CATALOGUE_PATH=PAYLOAD_FILE")
            loaded[path] = Path(filename).read_bytes()
        return loaded
    replacements = load_pairs(arguments.replace, "--replace")
    additions = load_pairs(arguments.add, "--add")
    report = rewrite_iso(arguments.iso, arguments.output, replacements or None, additions or None)
    print(json.dumps({
        "volume_size": report.volume_size,
        "catalogue_sha256": report.catalogue_sha256,
        "decoded_catalogue_sha256": report.decoded_catalogue_sha256,
        "physical_pages": report.physical_pages,
        "data_start": report.data_start,
        "source_sha256": report.source_sha256,
        "written_sha256": report.written_sha256,
        "identical": report.identical,
        "entries": len(report.entries),
        "replaced": [asdict(entry) for entry in report.entries if entry.replaced],
        "added": [asdict(entry) for entry in report.entries if entry.source_size == 0
                  and not entry.replaced],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
