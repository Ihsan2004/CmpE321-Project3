# Design Decisions

This document records the low-level format and sizing choices for our DBMS
engine, together with the reasoning behind each one. It is the basis for the
"sizing decisions" section the project asks us to justify in the report/video.

All multi-byte values are stored **little-endian**, with the byte order made
explicit in every `struct` format string (`<`), so the files are portable
across machines.

---

## 1. Field byte widths

| Field type | Stored width | How                                   |
|------------|--------------|----------------------------------------|
| `int`      | 4 bytes      | `struct.pack('<i', value)` — signed 32-bit |
| `str`      | 32 bytes     | ASCII bytes, null-padded (`\x00`)      |

**Integer = 4 bytes.** Signed 32-bit covers −2,147,483,648 .. 2,147,483,647.
Every integer value in our workloads (military strength, wealth, etc.) fits
comfortably. Going to 8 bytes would double index key size and shrink B+-tree
fanout for no benefit.

**String = 32 bytes.** Field *values* are capped at 32 characters. Shorter
values are right-padded with null bytes; on read we strip trailing nulls.
Because all string values are alphanumeric only (spec constraint), no value
contains an embedded `\x00`, so trailing-null stripping is unambiguous.
*Implication:* a string value longer than 32 chars is invalid input and is
rejected as a `failure`. 32 bytes is the same width used in the spec's worked
example and keeps records compact.

---

## 2. Page layout (slotted page, "unpacked" format)

Page size is `page_size` from config (default **4096 bytes**).

```
+--------------------------------------------------+  offset 0
|                 PAGE HEADER (16 B)               |
+--------------------------------------------------+  offset 16
|  slot 0   (record_size bytes)                    |
+--------------------------------------------------+
|  slot 1   (record_size bytes)                    |
+--------------------------------------------------+
|   ...  up to max_records_per_page slots ...      |
+--------------------------------------------------+
|                 free / unused space              |
+--------------------------------------------------+  offset 4096
```

### Page header — 16 bytes

| Bytes  | Field         | Type        | Meaning                              |
|--------|---------------|-------------|--------------------------------------|
| 0–3    | `page_id`     | `<i` int    | This page's own number (sanity/debug)|
| 4–7    | `record_count`| `<i` int    | Number of occupied slots             |
| 8–9    | `slot_bitmap` | `<H` uint16 | Bits 0..9 = slot occupied? (1 = used)|
| 10–15  | reserved      | 6 bytes     | Zero-filled padding                  |

"Unpacked" slotted format: slot *i* always lives at a fixed offset
`16 + i * record_size`, whether or not it is occupied. The bitmap tells us
which slots are live. This makes inserts/deletes O(1) on slot lookup and never
shifts records around.

### Capacity worked example — type `house`

`house` has 3 `str` + 3 `int` fields:

```
record_size  = 3*32 + 3*4            = 108 bytes
usable_space = 4096 - 16             = 4080 bytes
fits         = 4080 // 108           = 37 records
capacity     = min(37, max_records_per_page=10) = 10 records
used/page    = 10 * 108              = 1080 bytes  (rest stays free)
```

The `max_records_per_page` config value (≤ 10) is the hard cap; the geometric
fit is only an upper bound.

---

## 3. Free space tracking

Two *different* notions of "free", at two different layers — keeping each
layer a black box:

**Page-level (DiskSpaceManager).** Page 0 of every file is reserved as a
**file header page** owned by the Disk Space Manager. It stores:
- `num_pages` — total pages allocated in the file
- a **free-page list** — page numbers that were fully emptied and can be reused

So actual content (data or index nodes) lives in pages 1, 2, 3, … Upper layers
never see or touch page 0. `allocate` either pops a page id from the free list
or extends the file by one page.

**Slot-level (FileIndexManager).** Within a data page, the 10-bit
`slot_bitmap` plus `record_count` in the page header tell the File & Index
Manager which slots are free. To insert, it scans for a page with a free slot;
if none exists it asks the Buffer Manager (→ Disk) to allocate a new page.

We chose a **free list** over a global bitmap because whole-page deallocation
is rare in our workloads, so a short list is cheaper to maintain and store than
a file-wide bitmap.

---

## 4. File naming and locations

Everything lives in the **same directory as `archive.py`** (spec section 13).

| File              | Owner             | Purpose                               |
|-------------------|-------------------|----------------------------------------|
| `<type>.dat`      | DiskSpaceManager  | One relation's data pages              |
| `<type>.hidx`     | DiskSpaceManager  | One relation's hash-index pages        |
| `<type>.bidx`     | DiskSpaceManager  | One relation's B+-tree index pages     |
| `catalog.dat`     | FileIndexManager  | System catalog — all type metadata     |
| `log.csv`         | QueryProcessor    | Persistent, append-only operation log  |
| `output.txt`      | QueryProcessor    | Query + explain results                |
| `stats_output.txt`| QueryProcessor    | Statistics snapshots                   |

Relation files are created lazily when a `create type` command runs.

---

## 5. System catalog (approach — byte layout finalized in Step 4)

The catalog is a normal paged file (`catalog.dat`) accessed *through* the
Buffer Manager like everything else. For each type it records: type name,
field count, each field's name + type, and the 1-indexed primary-key
position. Index roots live in their own index files (`<type>.hidx` or
`<type>.bidx`) and are rebuilt from data on startup when an indexed strategy
is active. Because the catalog is just another paged file, it persists across
runs for free — satisfying the persistence requirement (spec section 15).

---

## 6. Summary of constants (single source of truth)

These will live in one place in code (e.g. a `constants.py` or the config) so
every layer agrees:

```
PAGE_SIZE            = 4096   (from config)
PAGE_HEADER_SIZE     = 16
INT_WIDTH            = 4
STR_WIDTH            = 32
MAX_RECORDS_PER_PAGE = 10     (from config, hard cap)
ENDIANNESS           = '<'    (little-endian, explicit in all struct formats)
```
