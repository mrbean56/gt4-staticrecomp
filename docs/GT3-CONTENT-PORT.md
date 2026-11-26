# GT3 → GT4Recomp content port

Owner request (2026-11-24): bring every GT3 car and circuit that GT4 does not have, plus
the GT3-era features, into this port. Scope rule: decision
[0077](decisions/0077-gt3-content-port-scope.md). Tasks: TASK-216 to TASK-219.

Everything below was read from two disc images, not from web lists:

| Disc | Where | Bytes | SHA-256 | Identity |
|---|---|---|---|---|
| GT3 USA v1.10 | owner's rip | 3,878,649,856 | `0946bb943a766d0afe057fbff5aa96132b16a797f3e3edc985a3b6d2f3afee0e` | PBPX_955.03, NTSC-U/C |
| GT4 USA v2.00 | owner's rip | 5,314,478,080 | `67b6c0075837f3ae1132d608acf2858bf13b2dd62d6eae83dff76df02e4e824f` | SCUS-97328, **matches `docs/inputs/usa-v2.00.json`** |

Tooling: [gt3tool](https://github.com/) (MIT, GT3-revisited) for the GT3 side, and this
repository's own `scripts/gt4_volume.py` for the GT4 side. The GT4 SpecDB tables were
read with the public GTDB/GTID/GTST layouts (Nenkai GT modding hub, "Database Formats").

## What is missing (verified content inventory)

GT4 USA v2.00 holds 722 `GENERIC_CAR` rows and 111 `crs/` course ids; GT3 USA v1.10
holds 244 cars and 19 circuits.

### Circuits: 6 of GT3's 19 have no GT4 course id

| GT3 circuit | GT3 id | GT4 |
|---|---|---|
| Complex String | `slalom80r` | absent |
| Rome Circuit | `rome` | absent |
| Smokey Mountain | `smtnorth` | absent (GT4 keeps Swiss Alps as `smtsouth`) |
| Special Stage Route 11 | `route11` | absent (GT4 has SSR5 `route5`) |
| Super Speedway | `super` | no named circuit; GT4's only oval is `crs/oval`, licence-test sized |
| Tahiti Circuit (dirt) | `tahitidirt` | absent (GT4 keeps Tahiti Maze) |

Shared: Apricot Hill, Deep Forest, Grand Valley, Midfield, Trial Mountain, Seattle,
Tokyo R246, Côte d'Azur, Laguna Seca, SSR5, Swiss Alps, Tahiti Maze, Test Course.

### Cars: 15 of GT3's 244 have no GT4 equivalent

| GT3 car | What GT4 has instead |
|---|---|
| Porsche 911 (996) GT3 | no Porsche at all (RUF only) |
| Lamborghini Diablo | no Lamborghini at all |
| F090/S · F094/H · F094/S · F686/M · F687/S · F688/S | one "Formula Gran Turismo `04" |
| Toyota GT-ONE Road Car (TS020) | the race car only |
| BMW 328ci | 330i / M3 |
| Mitsubishi FTO LM Race Car | road FTOs only |
| Mitsubishi Lancer Evolution VI GSR T.M.E. | VI GSR and VI RS |
| Mitsubishi Lancer (1999) | 1973/74 1600 GSR |
| Nissan Z Concept (2001) | 350Z and the LM concept |
| Tickford Falcon XR8 Race Car | road Falcon XR8 |

Every other GT3 car exists in GT4, often renamed (Vauxhall VX220 → Opel Speedster,
Suzuki ESCUDO Pikes Peak → ESCUDO Dirt Trial Car, Toyota ALTEZZA LM → ALTEZZA Touring
Car) or as another model year of the same car.

### Features

GT3 has no feature GT4 lacks: both run six-car grids and the same camera set. The
"32-car grids" and the camera editor belong to GT3-revisited, a modern engine rewrite,
not to the GT3 disc. What is worth having in this port is already planned or is a mod:
TASK-159 (360° chase camera), TASK-160 (FoV), and a larger field (TASK-219).

## Two discs at setup (owner, 2026-11-24)

Setup takes the player's GT4 image and their GT3 image. Everything GT3 supplies is read and
converted **on the player's own machine** into that player's GT4 data: car rows from GT3's own
`paramdb` (power, torque curve, weight, gearbox, dimensions, drivetrain, tire sizes — the same
columns GT4 stores), and geometry converted from GT3's model containers into GT4's serialized
form. Nothing converted is shipped, and a player without a GT3 image runs the reference game.

### What a GT4 car is, exactly

Every per-car row carries the car's code in its label, so a car is a **closed label set**, not a
row in one table. `scripts/gt4_car_map.py` enumerates it, and two unrelated cars agree on the
shape (Confirmed on the owner's disc, 2026-11-24):

| Group | Rows | Example |
|---|---|---|
| the car itself | 20 | `ka_01` in `GENERIC_CAR`, `CAR_NAME_*` (9 languages), `CAR_VARIATION_*` (9), `ARCADEINFO_NORMAL` |
| its own data, one row per letter | 67 | `ch_ka_01_a` (CHASSIS, one), `en_…` (ENGINE, one), `ge_…_a..d` (GEAR, four), `su_…_a..d`, `ft_…_a..m` (FRONTTIRE, thirteen), `rt_…` (REARTIRE, thirteen), `br_…` (two), `cl_…`/`fw_…`/`ls_…`/`mu_…`/`nt_…`/`lw_…` (threes), `eb_…`, `co_…`, `bc_…`, `rm_…`, `no_…`, `pp_…`, `tk_…` (twos) |
| class and event scoped (3 tables) | 8 | `pr_compact_ka_01` (DEFAULT_PARAM), `pt_compact_ka_01` (DEFAULT_PARTS), `gt4r_compact_ka_01` and `gt4sample_*_ka_01` (ENEMY_CARS) |

95 rows in 44 tables for `ka_01`, the same 95/44 for `m3_04`. The label prefixes are consistent
(`ch_` chassis, `en_` engine, `ge_` gear, `ft_`/`rt_` tyres, `pr_`/`pt_` class defaults, `gt4r_`
event entries), so **adding a trim car is a label clone**: copy a donor car's 87 car-owned rows
under the new code, append them with new row ids, and copy the 8 class/event rows only if the
new car belongs to the same class and events.

The GT3 side has the same shape, which is the good news for the port: GT3's `car` table (264 bytes,
one row per car, dumped by `gt3tool db` as `30_car.csv`) is a row of **label references** —
`br_s4_00_a`, `ch_s4_00_a`, `rm_s4_00_a`, `en_s4_00_a`, `tk_s4_00_a`, `dt_s4_00_a`, `cl_s4_00_a`,
`ls_s4_00_a`, `ge_s4_00_a`, `su_s4_00_a`, `ft_s4_00_a`, `rt_s4_00_a` — with the same two-letter
families and the same `<family>_<code>_<letter>` naming. A GT3 car therefore already arrives with
its whole row set labelled, and the port's work is per-table column conversion between two
databases that share the table taxonomy.

`scripts/gt4_car_clone.py` does the GT4 side of that: a plan and the written rows for one donor car
under a new code, with `--name` appending one string to each of the nine `<language>_StrDB.sdb`
tables and pointing the cloned `CAR_NAME_*` rows at it (`write_strings` reproduces all ten shipped
string tables byte for byte, Confirmed).

Two format facts the clone depends on (Confirmed): the row tables are searched by row id, so a
new row takes an id above that table's maximum and the row infos stay ascending; the label tables
are in shipped car order, not sorted, so their new keys go at the end.

That removes the distribution problem completely, but it does not remove the hard part: the
recompiled game executes GT4's own code, which builds per-car VIF packets for GT4's VU1
microcode, and GT3's models were authored for GT3's engine. A GT3 car has to arrive in GT4's
serialized model form before GT4 can draw it, wherever the conversion runs.

## The four pipelines

Content enters GT4Recomp only through GT4's own data files, so each pipeline has the
same shape: read the GT4 format, write it back byte-identically, then change one thing.

### A. Cars without new art (SpecDB rows) — TASK-216

A GT4 car is a `GENERIC_CAR` row + a `CAR_NAME_american` row + a `CAR_VARIATION` row
pointing at a `VARIATION` row whose model-code string index names an existing
`car/menu/<code>` and `car/lod/<code>` pair, plus a per-car index record at
`specdb/GT4_US2560/CARS/<row id>` (confirmed in game 2026-11-26: the Adhoc `MCarData`
natives resolve a code label to its row id and load this record; a label whose row id has
no record builds a null car and the VM jumps to address 0). Measured on the owner's disc (2026-11-24,
`gt4_car_map.car_catalogue`): **722 cars, 722 distinct model codes, 3,849 variation rows** —
one model per car and up to 31 paint variants on it (`dodg0007`), so no two cars share a
model by their default variation. A row-level port therefore *copies* a donor car's model
code rather than pointing at a shared one, and what it inherits is that donor's art.

A trim GT3 car that matches an existing GT4 body (Evo VI T.M.E., 1999 Lancer, 328ci)
is therefore a row-level addition: a new row ID with the right name and the existing
model code. No geometry, no textures, no model converter.

Identity and art are only part of a car. GT4 keeps the specifications in the same table
family GT3 uses — `CHASSIS`, `ENGINE`, `GEAR`, `DRIVETRAIN`, `BRAKE`, `SUSPENSION`,
`TIRESIZE`, `FRONTTIRE`, `REARTIRE`, `CLUTCH`, `FLYWHEEL`, `LSD`, `MUFFLER`, `WHEEL`,
`RACINGMODIFY`, … 79 tables in `GT4_US2560`, and `gt3tool db` already dumps GT3's
counterparts (`car`, `car_variant`, `chassis`, `engine`, `gear`, `front_tire`, …). So the
port is a per-table mapping between two databases with the same taxonomy, keyed by label
on both sides — not a translation from GT3's monolithic 264-byte car record.

What the disc says about the format (TASK-216 milestone 1, 2026-11-24): a row is a
*stream*, not a compressed byte run. It starts with a symbol count; count one copies a
row from a 64-row block pool, a larger count patches a template row (the first symbol
holds the template index in its low six bits, then a byte mask and the replacement
bytes). Huffman codes are LSB-first through a 256-entry prefix table plus a code list.
Every shipped table has version `0x00080001`/`0x00080003` and is still compressed, so
the public "bit `0x01` means uncompressed" reading does not hold here: whether the game
accepts an uncompressed table is **Unknown** until it is tried in game. The reader is
verified against the recorded oracle (`ka_01` row 59 → name index 84, slice 121's
eleven variation rows) and every table round-trips through the uncompressed writer with
identical rows, so the encoder is a fallback, not a prerequisite.

Milestone A3 (2026-11-24) makes one entry's decoded size change. The volume writer
re-encodes the catalogue from its decoded pages (header, XOR offset table, page
headers/tables/keys/metadata), keeps each allocated page count and the volume extent,
and re-frames a replacement with the entry's own kind; entries it does not replace
keep their bytes and their offsets unless a grown neighbour needs the space. Evidence:
`specdb/GT4_US2560/GENERIC_CAR.dbt` rebuilt as its 23,120-byte uncompressed table
(shipped entry 17,960 decoded) yields an image where all 18,352 paths enumerate, the
other 18,351 entries are byte-identical, and the table reads back as 722 rows with the
recorded row 59. The rebuilt image is `/tmp/opencode/gt4-rebuilt.iso` for the owner's
test. Whether the game accepts a rebuilt volume, and whether a catalogue whose stored
page sizes differ from the shipped ones loads, is still **Unknown**; that test is the
gate. Details and the two limits (catalogue allocation cannot grow, entry storage kind
cannot change) are in [the volume reader document](VOLUME-READER.md).

Milestones: decode every `GT4_US2560` table (oracle: `ka_01` row 59 carries name index
84, `CAR_VARIATION` row 444/table 42, model index 1598 — slice 120/121 evidence); write
a table back and reload it with identical rows; rebuild `GT4.VOL` with only that table
changed; add one trim car and see it in the garage and in Arcade.

**Where the roster comes from is settled** (2026-11-26, in game). The membership is data:
`projects/arcade/US_carlist.adc` (and the dealer's `projects/gtmode/US_carlist.adc`) holds
one list per maker as `u32 object id, 0x1D, u16 length, car code` entries. Replacing the
Ka's `ka_01` with `m3_04` in `ford_carlist` puts the BMW M3 '04 page under Ford with its
own data (`gt4-arcadelist-m3.iso`); the `Ford N cars` line is a separate `*_carnum` script
object, so it stayed at five. What the clone still lacks (High confidence) is the per-car
`CARS/<row id>` record: renaming the Ka's own label to `ka_02` (row ids and `CARS/…003B`
untouched, `gt4-row59-ka02.iso`) loads it normally, while a new row id has no record.
That record is now generated and inserted: it is `u32 count, u32 0` and 16-byte
`(row id, table id, letter, 0)` entries, the writer splits the full CARS leaf and its
index page to add the catalogue entry, and `gt4_car_clone.py` writes both. Data-side,
no build: TASK-222.

### B. Cars with new art — TASK-217

The other eight need a GT4 car model, which the repository has only read at header
level (slices 121–124: `CAR4`/`MDLS` containers, 4 models, 22/113 shapes, Tex1 slots,
packets framed to their QWC end; geometry, textures and relocation opaque).

Milestones: decode one full `CAR4` (VIF packets, vertices, texture transfer) and write
it back byte-identical; substitute one shape's vertices and see it in game; then convert
GT3 model data (`gt3tool car-export`, MIT, GT3 container documented by GT3-revisited)
into that written form, with wheels, LOD and the garage preview.

This is the long one. It is a research effort of months, not a task that lands whole,
and it needs the owner's Windows machine for every visible result.

### C. Circuits — TASK-218

GT4 courses are `crs/<id>` containers of 24–29 typed sections (`GTM1` models, `Vls0`
reflection/wet slots, scenery) plus `<id>.ad` race data, and a `COURSE` row. GT3's
courses are documented in full by GT3-revisited and already exportable to OBJ + PNG.

Milestones: decode one `crs/` file end to end (section table, road surface, AI line,
start grid), write it back byte-identical, then substitute a course's geometry with a
converted GT3 track. Order: Complex String first (pure geometry, no scenery), then
Rome Circuit, then the dirt and mountain tracks.

State (2026-11-26): the container is read and written for all 110 courses and all 110
`.ad` files (`scripts/gt4_course.py`, 17 controls). The `.ad` lines are decoded: `ADLN`,
eight polylines, a 0x30-byte header, a table of eight offsets, then one 0x40-byte entry
header per line and 0x40-byte records carrying a position at +0x04/+0x08 and a tangent
heading at +0x24 (a road heading within about 7 degrees at the ~130 m spacing, not an
exact tangent). Lines 1 and 2 are the road edges, 6 a second full-length line, 3 a third
of different density, 4 a short line across the start area, 7 a sparse line; 5 is empty.
Seventeen `.ad` files (photo spots, the gymkhana spirals, `testcourse_100m`) carry an
undecoded form and are kept verbatim. Section bodies (`MDLS`, `Vls0`, `Tex1`, …) are still
opaque, and the `CRSS` content is only partly named: a record index at +0x20/+0x24, four
sub-block offsets, world-space points that do not depend on race direction.

### D. Features — TASK-219

Bigger fields are an engine change (grid slots, opponent spawn, HUD position list,
replay, memory), which decision 0073 makes a mod: off by default, labelled, with the
reference behaviour intact when off.

## Licensing and distribution

Nothing from GT3 is redistributed. Ports are built at setup time from the player's own
GT3 disc, as GT3-revisited does and as decision 0069 already requires for GT4. The
repository ships tooling and the conversion code, never assets from either disc.

## Unknowns that decide the schedule

- How a car code becomes a car. Answered (2026-11-26): the roster is the per-maker lists
  in `projects/<mode>/US_carlist.adc`; a code label resolves through its `.idi` label to a
  row id, and the menu loads `specdb/GT4_US2560/CARS/<row id>`, a ~1.2 KB per-car record.
  A new row id without that record builds a null car and crashes (`ra 0x0031C8A8`). The
  record's layout and the catalogue insertion are implemented (TASK-222), and a label must
  go into its `.idi` table in (length, name) order because the engine binary-searches it:
  with both, the Ford page lists the clone in game and the race load names it. The race
  start is the last check.
- Whether a `GT4.VOL` rebuilt with changed sizes loads at all (Unknown; milestone A3).
- GT4 geometry, texture and relocation semantics (Unknown; slices 121–124 stop at
  headers).
- GT4 course section bodies (`MDLS`, `Vls0`, `Tex1`, …) are still Unknown; the container
  and the `.ad` polylines are read and written as of 2026-11-26.
- Realistic effort per pipeline (not measured).
