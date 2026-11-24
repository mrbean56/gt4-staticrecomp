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

## The four pipelines

Content enters GT4Recomp only through GT4's own data files, so each pipeline has the
same shape: read the GT4 format, write it back byte-identically, then change one thing.

### A. Cars without new art (SpecDB rows) — TASK-216

A GT4 car is a `GENERIC_CAR` row + a `CAR_NAME_american` row + a `CAR_VARIATION` row
pointing at a `VARIATION` row whose model-code string index names an existing
`car/menu/<code>` and `car/lod/<code>` pair. Slice 121 confirms 11 physical variations
share one model, so several cars already reuse one body.

A trim GT3 car that matches an existing GT4 body (Evo VI T.M.E., 1999 Lancer, 328ci)
is therefore a row-level addition: a new row ID with the right name and the existing
model code. No geometry, no textures, no model converter.

Unlocking fact: a `GTDB` table may be stored **uncompressed** (version flag `0x01`),
which the game accepts, so a writer needs the Huffman *decoder* and never an encoder.
`GENERIC_CAR` is 722 × 24 B = 17 KB; the whole USA database is a few hundred KB.

Milestones: decode every `GT4_US2560` table (oracle: `ka_01` row 59 carries name index
84, `CAR_VARIATION` row 444/table 42, model index 1598 — slice 120/121 evidence); write
a table back and reload it with identical rows; rebuild `GT4.VOL` with only that table
changed; add one trim car and see it in the garage and in Arcade.

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

### D. Features — TASK-219

Bigger fields are an engine change (grid slots, opponent spawn, HUD position list,
replay, memory), which decision 0073 makes a mod: off by default, labelled, with the
reference behaviour intact when off.

## Licensing and distribution

Nothing from GT3 is redistributed. Ports are built at install time from the player's
own GT3 disc, as GT3-revisited does and as decision 0069 already requires for GT4.
The repository ships tooling and the conversion code, never GT3 assets.

## Unknowns that decide the schedule

- Whether GT4's menus and save system enumerate cars from the SpecDB at runtime or from
  a compiled table (Unknown; the first trim car answers it).
- Whether a `GT4.VOL` rebuilt with changed sizes loads at all (Unknown; milestone A3).
- GT4 geometry, texture and relocation semantics (Unknown; slices 121–124 stop at
  headers).
- GT4 course section semantics (Unknown; nothing in the repository reads `crs/`).
- Realistic effort per pipeline (not measured).
