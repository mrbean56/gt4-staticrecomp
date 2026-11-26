# A Larger Starting Field Than Six (TASK-219)

2026-11-24. Owner request: every six-car race becomes a 24-car race, as an opt-in mod. This page records what
the disc and the game code actually say, the data half that is implemented, and the code sites still to settle.

## The field size is data, not a constant

Each race has one `RACE` SpecDB row, 151 bytes. Its byte at **offset 0x3E** is the field size — PDTools' `Race`
mapping names it `MaxEntries` between the two unnamed bytes at 0x3C/0x3F and `StartType` at 0x40. The shipped USA
v2.00 data (709 rows) uses:

| Value | Races | What they are |
|---|---|---|
| 6 | 537 | ordinary races |
| 2 | 86 | duels and two-car events |
| 1 | 85 | time trials and solo events |
| 4 | 1 | one four-car event |

Confirmed against the rows themselves: every row is 151 bytes and the bytes around the field read as the mapping
places them. `am_sunday_0001` ends `… 00 00 06 01 02 …`, i.e. the two zero `YearMin`/`YearMax` shorts at 0x38 and
0x3A, an unnamed zero short at 0x3C, `06` at 0x3E, `01` at 0x3F and `StartType` 2 at 0x40 — the column order the
mapping documents. The value itself is what settles it: 6 in ordinary races, 2 in two-car events, 1 in time trials.

## The opponent pool is not the grid

`specdb/GT4_US2560/RACES/<race row id>` is the candidate list. Its format is `[u32 count][u32 0]` then `count`
16-byte entries (car id, then the same shape for colour/index — PDTools' `RaceSpec`). All 828 files decode
cleanly, counts 0 to 29.

Of the 537 six-car races, 196 ship a pool **smaller** than six — down to one entry, e.g. `it_alfa_gta_0001`,
`ex_formula_1500`, `uk_mg_0001`. A one-car pool cannot mean a one-car race, so the engine repeats pool entries to
fill the field. Confirmed as a consequence, which is why raising the field size needs no pool change.

## What the game code does with it

Confirmed in the reconstructed ELF (`scripts/gt4_core_elf.py`, text SHA-1 `28422e7e…`):

- `RaceInformation__virtual_00` (0x3BB490) reads the row byte and raises its own counter:

  ```
  lbu  v1, 0x3e(s0)      ; MaxEntries
  lw   v0, 0x118(s0)     ; current count
  slt  v0, v0, v1
  beql v0, zero, +8
  sw   v1, 0x118(s0)     ; count = max(count, MaxEntries)
  ```

- three race-information constructors start that counter at 6 before the row is applied:
  `RaceSinglePlayerInformation__structor_0` (0x3D5AE0), `RaceGTmodeInformation__structor_0` (0x3EDBE0),
  `RaceChampionshipInformation__structor_0` (0x3EE5E4).

So the shipped 6 is a data value that the load path can raise, and the code already has the branch that does it.
**Hypothesis:** `+0x118` is the race's working capacity (entry list or grid), and the default of 6 is why a
`MaxEntries` of 1 or 2 still behaves. It is not established that the arrays behind that count are sized from it
rather than fixed at six.

## Six-wide loops that still need settling

`slti $x, 6` used as a loop bound, or `li $x, 6` as a count, in race code. Each is a candidate for the mod and
each is a Hypothesis until an in-game run says otherwise:

| Site | Function | Shape |
|---|---|---|
| 0x35EFA4 | `RaceDisplayLapDiffEvent__structor_0` | 6 iterations, 4-byte stride (a per-car float array) |
| 0x3ED74C | `RaceTraining__virtual_96` | 6 iterations, 12-byte stride, zeroing 2 words per step |
| 0x3DB1E8, 0x3DB2EC | `RaceMonitor__setState` | `for (i = 0; i < 6; i++)` around a container walk |
| 0x3EE7E8… | `RaceChampionshipInformation__virtual_13` | 12 separate `slti …, 6` guards |
| 0x3EE5E4 | `RaceChampionshipInformation__structor_0` | count = 6, then 5 in the same block |

Whether each of these is cars, gears (GT4 cars have six) or championship rounds is not settled; the addresses are
named so the patch is written against evidence instead of guessed offsets.

## The data half, implemented

`scripts/gt4_field_size.py` raises every row whose field is exactly six and leaves 1, 2 and 4 alone:

```
gt4_field_size.py --iso <disc.iso> --inspect
gt4_field_size.py --iso <disc.iso> --output <mod.iso> [--field-size 24]
```

The `RACE` table is re-serialized in the shipped dictionary form (`write_table(..., compressed=True)`) and stored
again through the volume writer; every other entry keeps its shipped bytes. Nine controls in
`tests/python/test_gt4_field_size.py` (six fixture ones, the real-disc histogram, the 537-row rewrite, and a
rebuilt-image check behind `GT4_FIELD_IMAGE`).

The first field image (`gt4-field24.iso`, 5,314,478,080 bytes, file SHA-256 `d948ce6d…`; the build reports
`60e7ca91…` for the `GT4.VOL` extent only) used the older uncompressed writer form and is superseded: see
"The writer form was wrong" below. Verified after the build, and still true of the rebuilt image:

- 18,352 catalogue paths, the same set, and 18,351 entries byte-identical at unmoved offsets;
- the only changed entry is `specdb/GT4_US2560/RACE.dbt` (112,747 decoded, 8,997 stored), which parses back with
  537 rows at 24 and 172 unchanged;
- bytes outside the `GT4.VOL` extent identical (216,840,192 before, 2,638,135,296 after; same file size).

## The switch

Decision 0073: the mod is off by default and the reference game is untouched when it is off. Nothing in this tool
runs on its own — the player's pinned image is the reference, and a field-expanded image is a separate file produced
by an explicit command. The later setup-time version (TASK-221) keeps the same rule: the conversion writes its own
copy, and skipping it leaves the reference data alone.

## The writer form was wrong (in-game runs, 2026-11-26)

Run on Bilboquet (Windows 11, RTX 3070 Ti) with the 1.3.0-rc16 game binary, the disc passed directly with `--disc`
and a copy of the launcher's memory card. Arcade, Single Race, Nürburgring Nordschleife, Ford Ka `01.

| Image | What happened |
|---|---|
| `gt4.iso` (shipped) | Entry list shows the six cars; race camera hook at vblank 8,889; no halt. The baseline. |
| `gt4-rebuilt.iso` | Boots to the mode menu. Arcade car screen reads `Type:--`, `Power:90HP/499.5ccm`, and refuses the car ("This car cannot be driven in Single Race mode"). Its only change is `GENERIC_CAR.dbt` re-serialized in the uncompressed writer form: the game misreads that form. |
| `gt4-field24.iso` (uncompressed writer form) | Race loaded to the camera hook (vblank 41,033), then halted 6 frames later: `no translated code at 0x00000000 (ra 0x003FD354)`. The call site is a virtual call through a null pointer: `lw a1,0x2880(s2); lw v1,8(a1); addiu v1,v1,0x58; lh a0,(v1); lw v0,4(v1); jalr v0`. |
| `gt4-race-control.iso` (shipped field values, uncompressed writer form) | Halted the same way at its own race start (camera hook vblank 16,097, halt at 16,103). So the halt is the uncompressed GTDB form, not the 24-car value. |
| `gt4-race-control2.iso` (dictionary writer form, shipped values) | Same six-car entry list as the baseline, no halt (camera hook vblank 7,717). The dictionary writer is validated in-game. |
| `gt4-field24b.iso` (dictionary form, rows at 24) | Six-car entry list, race runs, no halt (camera hook vblank 21,127; HUD 6/6). |
| `gt4-field24c.iso` (rows at 24 **and** 469 opponent pools cycled to 23 candidates) | Still six cars in the entry list and on the grid (camera hook vblank 8,475, no halt). |

What the runs settle:

- **The uncompressed GTDB writer form is not readable by the game.** Every table that matters must be written in the
  shipped dictionary form. `write_table(..., compressed=True)` does that now: 256 uniform eight-bit codes, one
  raw-row stream per row, and a block copy for rows longer than 254 bytes (`ALLOW_ENTRY`, 480-byte rows). All 79
  shipped tables round-trip through it (CTest, `test_gt4_specdb.py`).
- **The boot test alone does not validate an image.** `gt4-rebuilt.iso` reached the menus and looked healthy; the
  damage only showed on the car screen and at race start. A boot check proves the volume and catalogue, not the
  tables.
- **Arcade Single Race ignores both `MaxEntries` and the pool.** Raising the row to 24 and cycling its five
  candidates up to 23 changed nothing: the entry list and the grid stayed at six. Arcade's six is the code default in
  the race-information constructors (`RaceSinglePlayerInformation__structor_0`, 0x3D5AE0, which seeds `+0x118` at 6),
  so the owner's 24-car request needs the code half. The data half applies to GT Mode event rows (the 537 six-car
  `RACE` rows), which cannot be tested without a GT Mode save.
- The earlier note that "raising the field size needs no pool change" was wrong: the pool caps the candidates, and
  the data mod now raises both (537 rows, 469 pools).

Images rebuilt with the dictionary writer: `gt4-race-control2.iso` (file SHA-256 `e94d2c1d…`), `gt4-field24b.iso`
(file SHA-256 `15da5688…`) and `gt4-field24c.iso` (file SHA-256 `72d77666…`, rows and pools). The superseded ones
stay for the record.

## Test order for the owner

1. `gt4-race-control2.iso` — same RACE data as shipped, dictionary form. Done: six cars, no halt.
2. `gt4-field24b.iso` / `gt4-field24c.iso` — the field change. Done: still six cars in Arcade.
3. The code half: the six seeded at `+0x118` and the six-wide loops listed above, patched in the recompiled units and
   built on Windows, then the same Arcade race should show 24. GT Mode rows can be checked in parallel once there is a
   GT Mode save.

The launcher pins the disc hash, so a candidate is run by the game binary directly:
`gt4.exe SCUS_973.28 --ntsc --disc <iso> --memcard <card> --window`.

## What is Unknown

- Whether a 24-car field spawns 24 cars, and what the position board, results, replay and HUD do with the extra
  actors.
- Guest RAM per additional car, and therefore the ceiling, if any.
- Where the extra grid slots come from. The `RACE` row carries five start-position shorts (0x2E..0x37 in the mapping's
  order), and the six-car rows read zero there; whether a course computes its grid from its own start line or takes
  slots from this row is not settled, so 24 cars may share six grid positions until it is.
