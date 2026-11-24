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

Confirmed against the rows themselves: `am_sunday_0001` (`80 00 00 00 … 06 01 02 …`) has `06` at 0x3E, and the
ranges around it read as the documented columns (minutes at 0x10, prizes or start positions at 0x20, `StartType` = 2
at 0x40).

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

The `RACE` table is re-serialized in the uncompressed writer form and stored again through the volume writer; every
other entry keeps its shipped bytes. Nine controls in `tests/python/test_gt4_field_size.py` (six fixture ones, the
real-disc histogram, the 537-row rewrite, and a rebuilt-image check behind `GT4_FIELD_IMAGE`).

Image built from the owner's disc: `~/opencode/gt4/gt4-field24.iso`, 5,314,478,080 bytes, SHA-256
`60e7ca91bffb117229c1fef4f086824cc10fed9ec2f32dab5cbbd56cd9469f57`. Verified after the build:

- 18,352 catalogue paths, the same set, and 18,351 entries byte-identical at unmoved offsets;
- the only changed entry is `specdb/GT4_US2560/RACE.dbt` (112,747 decoded, 8,997 stored), which parses back with
  537 rows at 24 and 172 unchanged;
- bytes outside the `GT4.VOL` extent identical (216,840,192 before, 2,638,135,296 after; same file size).

## What is Unknown

- Whether the game boots this image at all: `RACE.dbt` is now stored through the writer's uncompressed GTDB path,
  the same unproven path as the earlier `gt4-rebuilt.iso`. One boot settles both.
- Whether a 24-car field spawns 24 cars, and what the position board, results, replay and HUD do with the extra
  actors.
- Guest RAM per additional car, and therefore the ceiling, if any.

The next step is therefore the boot test on the owner's machine, not more static work.
