# 0077 — GT3 content port: scope, order and evidence

Date: 2026-11-24. Owner request: every GT3 car and circuit GT4 lacks, plus the GT3-era
features, brought into this port. Written by the agent after reading both discs;
numbers, ids and hashes are in [GT3 content port](../GT3-CONTENT-PORT.md).

## Decision

1. **Target list comes from the discs, never from web lists.** GT4 USA v2.00 is the
   pinned input (`docs/inputs/usa-v2.00.json`, `67b6c007…`); GT3 USA v1.10 is the
   owner's own rip (`0946bb94…`). Any future check re-reads both rather than trusting a
   roster page: the public JP and US lists differ enough to invent gaps.
2. **Setup takes two discs: the player's GT4 image and their GT3 image** (owner,
   2026-11-24). Content is converted on the player's machine, from their own discs,
   and never redistributed — the same rule decision 0069 already applies to GT4 and
   GT3-revisited applies to GT3. The repository ships tooling and conversion code,
   never assets from either disc. A player without a GT3 disc simply gets the
   reference game.
3. **Order is by cost, not by visibility**: row-level cars first (TASK-216), then course
   containers (TASK-218), then new car art (TASK-217), then the larger field (TASK-219).
   A trim car that reuses an existing GT4 body is the only part that can land soon; the
   other three are research pipelines with their own gates.
4. **No pipeline skips its identity gate.** Each format is read, written back
   byte-identically and reloaded before any content changes, and every visual result is
   the owner's in-game test, per AGENTS.md.
5. **GT3 has no missing feature to port.** Six-car grids and the same camera set exist
   in both games; "32-car grids" and a camera editor are GT3-revisited features, not GT3
   ones. Camera work stays with TASK-159/160, and a bigger field is a mod under 0073.
6. **Faithful by default.** Ported cars and circuits are opt-in content; with them
   absent the game is the reference build. Nothing here changes base game logic.

## Why it is recorded as tasks instead of a branch

The four pipelines differ by an order of magnitude in effort and risk, and each needs
its own automated checks and owner test. Recording them keeps `TASKS.md` the single
ledger, so nobody mistakes a study for a finished port.
