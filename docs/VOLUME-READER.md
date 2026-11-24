# Bounded GT4 Volume Reader

Slice 120 adds offline `scripts/gt4_volume.py` with hand-packed controls in
`tests/python/test_gt4_volume.py`. It supports the pinned USA v2.00 ISO only.

## Contract

The tool hashes the whole ISO against the unchanged disc pin before opening
ISO9660 on the same handle. It checks GT4.VOL size/position, then both stored
and decoded catalogue hashes against slice 119. Whole-ISO hashing is separate
from the 2 MiB input and decoded catalogue caps. It validates page framing,
record bounds/types, link/index ranges, sole root, unique siblings/directory
IDs, connected parent graph, cycles/depth and every file extent.

This enumerates leaves. B-tree routing, reciprocal link ordering, exhaustive
malformed-format coverage and an independent PS2 oracle remain **Unknown**.
Non-final ToC segments accept the pinned flush suffix after bounded raw DEFLATE,
then XOR decoded bytes by `0x55`; full DEFLATE grammar/producer flush policy
are not qualified. See [primary sources](NENKAI-REFERENCES.md).

Extraction requires explicit catalogue paths: eight maximum, 16 MiB per file
and 32 MiB aggregate, for both stored and decoded data. All selected extents
and payloads pass before output creation. Kind 1 supports exact-length raw data,
rejecting PS2ZIP markers as unqualified. Kind 2 supports PS2ZIP only: matching
size complement, bounded raw DEFLATE, exact output size, EOF and no trailing
input. Other codecs/combinations fail closed. This is a supported subset.

Output is a fresh ignored capsule below this checkout's `private/`. Traversal,
existing capsules and symlink/junction ancestors are rejected. Generated flat
filenames avoid using game paths as host write paths; file opens are exclusive.
This assumes a trusted local worktree, not hostile concurrent path replacement
or TOCTOU resistance. Failures preserve partial capsules; no old data is deleted.
Assets, complete catalogues and extracted payloads remain private.

`rebuilt_volume` opens an image the writer produced, where only the GT4.VOL
extent (LBA and length) can be checked; `read_catalogue(stream, pinned=False)`
skips the whole-disc and slice119 pins for that verification path only. The
default reader still refuses anything but the pinned disc.

## Writer: re-encoded catalogue and resized replacement

Milestone A3 adds `scripts/gt4_volume_write.py` (standard library only; no pycdlib).
It verifies the whole-disc pin, rebuilds the GT4.VOL region, and copies the image:

```bash
python3 scripts/gt4_volume_write.py --iso gt4.iso --output rebuilt.iso \
  --replace specdb/GT4_US2560/GENERIC_CAR.dbt=GENERIC_CAR.dbt
```

The replacement file holds the entry's decoded payload bytes, of any length up to
16 MiB. It is re-framed with the entry's own kind (raw run, or PS2ZIP: magic, size
complement, raw DEFLATE). An unchanged payload keeps its shipped stored bytes, so
a rewrite that changes nothing reproduces the 2,459,502,592-byte region byte for
byte (SHA-256 `49f1a67a...3688e1c`).

The catalogue is written from its decoded pages: the 64-byte header with the new
stored size, the XOR-encoded page offset table, and every page's header, entry
table, keys and metadata. A page whose records did not change keeps its shipped
stored bytes; a page whose record moved or resized is re-encoded and raw-DEFLATED
with a final block. Byte identity of the ToC is not a goal, but its contents must
re-read identically: re-encoding all 407 pages of the shipped catalogue and
parsing them back returns the same 18,352 paths, offsets, kinds and sizes.

What the encoder does not recompute, and why:

- a page's unused bytes are stale, not zero-filled, and are carried verbatim;
- the four bytes before each page's entry table (a data watermark on leaf pages, a
  page number of Unknown meaning on index pages) are carried verbatim;
- index pages keep their keys, links and children untouched: routing is not
  implemented, so nothing that would need it is changed;
- an entry keeps its node kind and metadata length, so a replacement changes its
  payload and sizes, never how the entry is stored;
- the catalogue keeps its allocated page count (179 = 366,592 bytes). A catalogue
  that no longer fits is refused rather than grown, so `data_start` (17,850,368)
  never moves. Growing the allocation is not implemented.

Payloads are laid out in their original order at page-aligned offsets, keeping the
shipped offset when it is still free; a grown entry pushes the entries after it.
The volume keeps its shipped extent: a layout that would not fit is refused. A
moved entry leaves its shipped bytes in an unreferenced gap, so a rebuilt volume
can hold stale copies of replaced payloads.

Evidence (TASK-216 milestone A3, 2026-11-24): the uncompressed
`specdb/GT4_US2560/GENERIC_CAR.dbt` is 23,120 decoded bytes where the shipped entry
is 17,960. The rebuilt image enumerates all 18,352 paths, the other 18,351 entries
carry identical stored bytes (18,338 also compared decoded; 13 exceed the 16 MiB
decoded cap and are compared stored), the replaced entry decodes to the new table,
and `gt4_specdb.parse_table` reads 722 rows of 24 bytes with row 59
`b00a00002400000078320000d107030d0d00000000000000`. Its stored size shrank
11,743 -> 8,984, so no other entry's offset moved; relocation is covered by
synthetic controls. Whether the game accepts the rebuilt image, and a catalogue
whose stored page sizes differ from the shipped ones, remains **Unknown** until
the owner runs it.

## Usage

Choose an unused capsule each time:

```powershell
private/tooling-venv/Scripts/python.exe scripts/gt4_volume.py `
  --iso "Gran Turismo 4 (USA) (v2.00).iso" `
  --select specdb/GT4_US2560/GENERIC_CAR.idi `
  --select specdb/GT4_US2560/CAR_NAME_american.dbt `
  --select specdb/GT4_US2560/american_StrDB.sdb `
  --output private/new-one-car-identity
```

Private `manifest.json` maps flat filenames to catalogue paths and records
extents, kinds, sizes and stored/decoded SHA-256 hashes. No full catalogue is
written. Never use extraction capsules as mutable card/save directories.

## Evidence

**Confirmed:** public reader matches both slice 119 hashes and 407 pages,
18,562 records, 210 directories and 18,352 files. Five selected SpecDB resources
were extracted into a fresh capsule; all are kind 2 with validated PS2ZIP.
[Slice 120 metadata](inputs/slice120-car-identity.json) records hashes/extents
and subsequent identity evidence.

**Confirmed:** 22 synthetic tests pass; baseline-candidate CTest 3/3, Python
81/81, no skips. Tests cover framing, malformed headers/pages/records, codecs,
truncation/trailing input, caps, graphs, pins, selection and private output.
No runtime/shader/C++ change or new native build was needed. Offline extraction
does not qualify live car ownership, geometry/textures or drawing replacement.
P1 remains open; game sessions, cards, rendering, timing and input are unchanged.

Public source/test hashes identify canonical Git blob bytes with LF endings;
Windows CRLF checkout conversion is not a source change. Asset, ISO and private
artifact hashes remain exact byte hashes, without normalization.
