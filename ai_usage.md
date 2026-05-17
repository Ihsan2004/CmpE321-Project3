# AI Usage

This document discloses our use of AI tools during the project, per the spec
section 18 requirement: "Honest disclosure is not penalized. You must be able
to explain every part of your code."

## Tool used

Anthropic's Claude (model: Claude Opus 4.7), via the Claude.ai chat interface.

## Workflow

We worked through the project in a step-by-step pair-programming style with
Claude. The full plan was decided up front:

1. Project skeleton + module wiring (`archive.py`, four package folders).
2. Design of the `Result` dataclasses (the inter-layer contract).
3. Design decisions (`DESIGN.md`): byte widths, page-header layout, free
   space strategy, file naming.
4. `DiskSpaceManager`, `BufferManager`, then `FileIndexManager` in four
   sub-steps (schema/serialization → slotted pages + heap_scan → hash_index
   → B+ tree), then `QueryProcessor`, then workload generator and
   experiments.

For each step we did the same loop:

- **Discuss the design.** We named the goals, listed the open questions
  (e.g. "free list vs. bitmap?", "where does the catalog live?", "how do we
  detect a duplicate primary key under hash_index?"), and chose an approach
  with explicit trade-offs.
- **Write the code.** Claude drafted code at our direction, and we read,
  questioned, and edited it before it went into the project.
- **Write checks first / alongside.** We used small layer-focused check scripts
  while developing and kept the reproducible experiment runner in the final
  submission. Failing checks caused us to revisit the design rather than patch
  around the bug.

## Concrete examples of things we asked Claude

- Spotting a tension in the spec text (section 4.3 says identifiers are
  "alphanumeric only", but the spec's own sample uses `military_strength`
  with an underscore). We chose to accept underscores in names but enforce
  strict alphanumeric for string *values*, and documented why.
- Choosing fanout for the B+ tree. We capped per-node entries at
  `max_records_per_page` (default 10) so the tree actually has multiple
  levels in the project's workloads — otherwise the natural fanout from
  a 4096-byte page would keep everything in a single root for any plausible
  N, and the experiments wouldn't show B+ behaviour.
- Designing the on-disk format for `catalog.dat` (one page, num_types
  prefix, schemas packed back-to-back).
- Debugging a B+ tree bug where the new internal root was being allocated
  but its body was never written (the original `_alloc_node` tried to write
  an empty internal node, which violated the n-keys-imply-(n+1)-children
  invariant and tripped an assertion). Claude proposed the fix: only
  pre-initialise leaves; require callers to write internals immediately
  after allocation.
- Picking buffer-pool sizes (4, 8, 16, 32, 64) and workload sizes (200/500
  records) that would produce a *visible* hit-rate curve and a clean
  sequential-flooding result in Experiment 1.

## What was NOT delegated

- The architectural decisions (four-layer black box, Result-typed
  inter-layer calls, logical-vs-physical page numbering, persistence via
  catalog page + per-type files) were ours, with Claude helping us think
  them through and write them up.
- Every line of code was read by us before being committed. We can explain
  any of it on request.
- The experiment design, what to measure, and the discussion of *why* each
  result looks the way it does (sequential flooding, hash vs B+ trade-offs,
  buffer-size sensitivity) was our analysis.

## What we did NOT do

- We did not paste Claude's output unchanged without reading it.
- We did not have Claude generate the individual contribution reports —
  those are written by each team member.
- We did not use Claude to evade any spec requirement; where the spec was
  ambiguous (e.g. underscores in identifiers, default bucket count for
  hash_index, fanout for B+ tree) we made and documented an explicit
  choice.

## Reproducibility note

All design rationale lives in `DESIGN.md`, and step-by-step experiment
commands live in `record.txt`. Anyone with the same `config.json`,
`workload_generator.py` seed, and engine source should reproduce our
experiment numbers.
