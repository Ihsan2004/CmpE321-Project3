# Modular DBMS Engine — CMPE 321 Project 3

A modular, four-layer database engine in pure Python. Every layer is a black
box that talks to its neighbours only through `Result` objects (see
`results.py`). Configuration flips one line in `config.json` and the engine
behaves correctly under the new policy/index — no other code changes.

## Quick start

```sh
python3 archive.py config.json input.txt
```

Outputs (all next to `archive.py`):

| File              | What it is                                    |
|-------------------|-----------------------------------------------|
| `output.txt`      | Query results + `explain` blocks (per run)    |
| `log.csv`         | Persistent, append-only operation log         |
| `stats_output.txt`| Statistics snapshot (overwritten on `stats`)  |
| `<type>.dat`      | Data pages for relation `<type>`              |
| `<type>.hidx`     | Hash-index pages for `<type>` (when `index_strategy=hash_index`) |
| `<type>.bidx`     | B+-tree pages for `<type>` (when `index_strategy=bplus_tree`)   |
| `catalog.dat`     | System catalog                                |

## File layout

```
your_submission/
├── archive.py                  # entry point (DO NOT EDIT — exact template)
├── config.json                 # active config
├── results.py                  # the six Result dataclasses
├── workload_generator.py       # produces experiment input files
├── run_experiments.py          # automates the three experiments
├── record.txt                  # reproducible commands + results
├── DESIGN.md                   # sizing decisions, page layouts, rationale
├── README.md                   # this file
│
├── disk_space_manager/         # Layer 1 — only layer that does file I/O
│   ├── __init__.py             # exports DiskSpaceManager
│   └── disk_space_manager.py
│
├── buffer_manager/             # Layer 2 — LRU/MRU page cache
│   ├── __init__.py             # exports BufferManager
│   └── buffer_manager.py
│
├── file_index_manager/         # Layer 3 — records, pages, indexes
│   ├── __init__.py             # exports FileIndexManager
│   ├── file_index_manager.py   # the main class
│   ├── schema.py               # record + catalog serialization
│   ├── page.py                 # slotted-page byte operations
│   ├── catalog.py              # system catalog
│   ├── hash_index.py           # static hash on PK
│   └── bplus_tree.py           # B+ tree on PK
│
└── query_processor/            # Layer 4 — parses input, writes outputs
    ├── __init__.py             # exports QueryProcessor
    └── query_processor.py
```

## Configuration (`config.json`)

```json
{
  "page_size": 4096,
  "max_records_per_page": 10,
  "buffer_pool_size": 16,
  "replacement_policy": "LRU",
  "index_strategy": "bplus_tree"
}
```

| Field | Accepted values |
|-------|-----------------|
| `page_size` | bytes per page (default 4096) |
| `max_records_per_page` | per-page record cap, ≤ 10 |
| `buffer_pool_size` | number of in-memory frames |
| `replacement_policy` | `"LRU"` or `"MRU"` |
| `index_strategy` | `"heap_scan"`, `"hash_index"`, or `"bplus_tree"` |

## Supported commands (in `input.txt`)

```
create type <name> <num-fields> <pk-order> <f1-name> <f1-type> ...
create record <type> <v1> <v2> ...
delete record <type> <pk-value>
search record <type> <pk-value> [extras...]
range_search <type> <field> <low> <high>
explain <any DML command>
stats
stats reset
```

Notes:
- `pk-order` is 1-indexed (e.g. `1` means the first field is the primary key).
- Field types are only `int` and `str`. Min 6 fields per type.
- Type/field names: alphanumeric + underscore. String values: strictly
  alphanumeric (spec section 4.3).
- `range_search` works only on `int` fields. On a `str` field or a missing
  field it returns failure.
- `range_search` on the **primary key** uses the B+ tree fast path when
  `index_strategy = "bplus_tree"`. On non-PK int fields it falls back to
  heap_scan even under B+ tree. `hash_index` always falls back to heap_scan
  for range queries (spec section 7.2).
- Failures never crash the engine; they are logged as `failure` in `log.csv`
  and produce no line in `output.txt`.

## Reproducing the experiments

The fastest path:

```sh
python3 run_experiments.py
```

This generates every workload, runs `archive.py` with each required config,
parses `stats_output.txt`, and prints three Markdown tables (also written to
`experiment_results.md`). See `record.txt` for the per-step manual commands
and a discussion of each table.

To produce a single workload manually:

```sh
python3 workload_generator.py --mode random --records 200 --queries 50 \
    --seed 42 > my_workload.txt
```

Modes: `sequential`, `random`, `range`, `mixed`. Add `--int-pk` to use an
integer primary key (needed for the B+ tree fast path on range queries).

## Persistence

All state — data files, indexes, the system catalog, and `log.csv` — survives
a restart. If you stop the engine and re-run `archive.py` with the same data
directory, it recovers from disk. If you switch `index_strategy` between
runs, the engine notices and rebuilds the index file from the data on first
startup.

## Constraints (per spec section 14)

- Pure Python standard library. No third-party packages.
- All identifiers and string values are ASCII alphanumeric (string *values*
  strictly; identifiers also accept underscore — the spec's own sample uses
  `military_strength`, which has an underscore).
- All multi-byte integers are little-endian (`<i` in `struct`).
- Field byte widths: `int` = 4 B (signed 32-bit), `str` = 32 B (null-padded).
- Slotted page format with a 16-byte header (page_id, record_count,
  slot_bitmap) and fixed slot offsets. See `DESIGN.md` for the full layout.
