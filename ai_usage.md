# AI Usage

Per spec section 18.

## Tool used

Anthropic's Claude (via claude.ai chat interface).

## What we asked

We used Claude as a design sounding board and code-review aid: discussing
trade-offs (free list vs bitmap, B+ tree fanout cap, hash bucket count),
clarifying ambiguous spec wording (e.g. underscores in identifiers vs strict
alphanumeric values), and drafting boilerplate after we agreed on an
approach.

## What we changed

Every line in the submission was read, understood, and edited by us before
being committed. We can explain any part of the code on request. Design
decisions (the four-layer architecture, Result-typed inter-layer contract,
slotted page layout, catalog format, experiment setup and analysis) were
ours; Claude helped us think them through and write them up. Contribution
reports were written by each member directly.
