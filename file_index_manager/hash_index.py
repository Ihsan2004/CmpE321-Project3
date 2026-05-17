"""Step 4c -- Static hash index on primary key.

Used only for equality lookups (search_by_pk and duplicate-PK check). Range
queries fall back to heap_scan -- handled in FileIndexManager.

On-disk structure (one `<type>.hidx` file per type, managed by DSM)
-------------------------------------------------------------------
* Logical page 0 -- DIRECTORY. 64 buckets, each entry = int32 logical page id
  of the bucket's first page (or -1 if the bucket is empty).
* Logical pages 1..N -- BUCKET PAGES. Each holds up to `cap` index entries
  plus a `next_page_id` pointer for overflow chaining. Layout below.

Why 64 buckets? It's a fixed compromise: small enough to keep the directory
in one page with room to spare, large enough that for the workload sizes in
this project (a few hundred records) most buckets stay at one page. 64 is
not exposed in config because the project's config schema (spec section 6)
doesn't include a bucket count; if you change it, just rebuild the .hidx.

Bucket page layout (page_size bytes)
------------------------------------
    bytes 0..3   : page_id           (sanity)
    bytes 4..7   : entry_count
    bytes 8..11  : next_page_id      (-1 if no overflow page)
    bytes 12..15 : padding
    bytes 16..   : entries back-to-back, each `entry_size` bytes

Entry format (depends on PK type, fixed once per type)
-------------------------------------------------------
    int PK:  4 B key  | 4 B data_page_id | 4 B slot_id  = 12 B
    str PK:  32 B key | 4 B data_page_id | 4 B slot_id  = 40 B

Deletion compacts the page (move last entry into the deleted slot), so we
never need a bitmap -- just `entry_count` and the implicit back-to-back order.

What we count
-------------
Every bucket page we visit -- read OR write -- bumps the FileIndexManager's
`index_nodes_visited` counter, which feeds the stats output.
"""

import struct
from typing import Optional, Tuple, List

from .schema import Schema, INT_WIDTH, STR_WIDTH

# ---- tunables (single source of truth) ---------------------------------
NUM_BUCKETS = 64

# bucket-page header
_BHDR_FMT = "<iii4x"           # page_id, entry_count, next_page_id, 4 pad
_BHDR_SIZE = struct.calcsize(_BHDR_FMT)   # 16 bytes

# directory layout (logical page 0 of .hidx)
_DIR_ENTRY_FMT = "<i"
_DIR_ENTRY_SIZE = struct.calcsize(_DIR_ENTRY_FMT)   # 4 bytes
_DIR_TOTAL_SIZE = NUM_BUCKETS * _DIR_ENTRY_SIZE     # 256 bytes


def _entry_size(schema: Schema) -> int:
    """Bytes per index entry for this schema's primary key."""
    _, pk_type = schema.primary_key_field
    key_w = INT_WIDTH if pk_type == "int" else STR_WIDTH
    return key_w + 4 + 4   # key + data_page_id + slot_id


def _pack_entry(schema: Schema, key, data_page_id: int,
                slot_id: int) -> bytes:
    _, pk_type = schema.primary_key_field
    if pk_type == "int":
        return struct.pack("<iii", int(key), data_page_id, slot_id)
    # str: 32 byte null-padded ASCII key
    b = key.encode("ascii")
    if len(b) > STR_WIDTH:
        raise ValueError(f"key too long: {key!r}")
    return (b + b"\x00" * (STR_WIDTH - len(b))) + struct.pack(
        "<ii", data_page_id, slot_id
    )


def _unpack_entry(schema: Schema, blob: bytes) -> Tuple:
    _, pk_type = schema.primary_key_field
    if pk_type == "int":
        key, dp, sl = struct.unpack("<iii", blob)
        return key, dp, sl
    raw = blob[:STR_WIDTH]
    key = raw.rstrip(b"\x00").decode("ascii")
    dp, sl = struct.unpack_from("<ii", blob, STR_WIDTH)
    return key, dp, sl


def _hash_key(key, num_buckets: int = NUM_BUCKETS) -> int:
    """Stable, deterministic hash. Python's built-in hash() is salted per-run
    starting 3.3+, so we roll our own based on bytes. Endianness is consistent
    via struct."""
    if isinstance(key, int):
        # Map negatives into the positive range, modulo a big prime, then mod
        # num_buckets. Cheap and well-distributed for the project's int range.
        return (key * 2654435761 % (2**32)) % num_buckets
    # str: sum-of-bytes weighted by position. Not cryptographic; "spreads"
    # short alphanumeric keys enough for our workloads.
    b = key.encode("ascii")
    h = 0
    for i, ch in enumerate(b):
        h = (h * 131 + ch) % (2**32)
    return h % num_buckets


class HashIndex:
    """Static hash index. Built once at create_type, maintained on every
    insert/delete, consulted on every equality lookup."""

    def __init__(self, schema: Schema, buffer, disk, page_size: int):
        self.schema = schema
        self.buffer = buffer
        self.disk = disk
        self.page_size = page_size
        self.file_id = f"{schema.name}.hidx"
        self.entry_size = _entry_size(schema)
        # Capacity per bucket page: bytes after header / entry size.
        self.entries_per_page = (page_size - _BHDR_SIZE) // self.entry_size
        # Counter exposed to FIM; FIM aggregates into its own counter.
        self.nodes_visited = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def build(self) -> None:
        """Initialise an empty index file (called on `create type`)."""
        if not self.disk.file_exists(self.file_id):
            self.disk.create_file(self.file_id)
        # logical page 0 = directory
        self.disk.allocate_page(self.file_id)
        empty_dir = bytearray(self.page_size)
        # all -1
        for i in range(NUM_BUCKETS):
            struct.pack_into(_DIR_ENTRY_FMT, empty_dir,
                             i * _DIR_ENTRY_SIZE, -1)
        self.buffer.write_page(self.file_id, 0, bytes(empty_dir))

    # ------------------------------------------------------------------
    # Directory helpers
    # ------------------------------------------------------------------
    def _get_bucket_head(self, bucket: int) -> int:
        """Return the logical page id of the bucket's first page, or -1."""
        bres = self.buffer.get_page(self.file_id, 0)
        self.nodes_visited += 1
        try:
            (head,) = struct.unpack_from(
                _DIR_ENTRY_FMT, bres.page.data, bucket * _DIR_ENTRY_SIZE
            )
            return head
        finally:
            self.buffer.unpin(self.file_id, 0, dirty=False)

    def _set_bucket_head(self, bucket: int, page_id: int) -> None:
        bres = self.buffer.get_page(self.file_id, 0)
        self.nodes_visited += 1
        try:
            buf = bytearray(bres.page.data)
            struct.pack_into(_DIR_ENTRY_FMT, buf,
                             bucket * _DIR_ENTRY_SIZE, page_id)
            self.buffer.write_page(self.file_id, 0, bytes(buf))
        finally:
            # write_page handles pinning internally; unpin one extra (the
            # outer get_page's pin). write_page itself unpinned its own pin.
            self.buffer.unpin(self.file_id, 0, dirty=True)

    # ------------------------------------------------------------------
    # Bucket page helpers
    # ------------------------------------------------------------------
    def _read_bucket_page(self, page_id: int):
        bres = self.buffer.get_page(self.file_id, page_id)
        self.nodes_visited += 1
        page = bres.page.data
        _, count, nxt = struct.unpack_from(_BHDR_FMT, page, 0)
        return page, count, nxt

    def _write_bucket_page(self, page_id: int, count: int, nxt: int,
                           entries_blob: bytes) -> None:
        buf = bytearray(self.page_size)
        struct.pack_into(_BHDR_FMT, buf, 0, page_id, count, nxt)
        buf[_BHDR_SIZE:_BHDR_SIZE + len(entries_blob)] = entries_blob
        self.buffer.write_page(self.file_id, page_id, bytes(buf))

    def _alloc_bucket_page(self, next_page_id: int = -1) -> int:
        alloc = self.disk.allocate_page(self.file_id)
        # immediately write a valid empty header
        self._write_bucket_page(alloc.page_id, 0, next_page_id, b"")
        return alloc.page_id

    # ------------------------------------------------------------------
    # Public API used by FileIndexManager
    # ------------------------------------------------------------------
    def insert(self, key, data_page_id: int, slot_id: int) -> None:
        """Add an index entry. Caller must already have verified no duplicate
        primary key exists (lookup() returns None for `key`)."""
        bucket = _hash_key(key)
        head = self._get_bucket_head(bucket)

        if head == -1:
            # bucket empty -> allocate first page
            new_pid = self._alloc_bucket_page(next_page_id=-1)
            self._set_bucket_head(bucket, new_pid)
            head = new_pid

        # Walk the chain to find a page with room; if every page is full,
        # allocate a new one at the head.
        prev_pid = None
        cur_pid = head
        while cur_pid != -1:
            _, count, nxt = self._read_bucket_page(cur_pid)
            self.buffer.unpin(self.file_id, cur_pid, dirty=False)
            if count < self.entries_per_page:
                # has room
                self._append_entry(cur_pid, key, data_page_id, slot_id)
                return
            prev_pid, cur_pid = cur_pid, nxt

        # Chain is fully packed -> new page at the head, pointing to old head
        new_pid = self._alloc_bucket_page(next_page_id=head)
        self._set_bucket_head(bucket, new_pid)
        self._append_entry(new_pid, key, data_page_id, slot_id)

    def _append_entry(self, page_id: int, key, data_page_id: int,
                      slot_id: int) -> None:
        page, count, nxt = self._read_bucket_page(page_id)
        # extract existing entries
        body = bytearray(self.page_size - _BHDR_SIZE)
        body[:count * self.entry_size] = page[_BHDR_SIZE:_BHDR_SIZE + count * self.entry_size]
        # add new entry
        new_blob = _pack_entry(self.schema, key, data_page_id, slot_id)
        off = count * self.entry_size
        body[off:off + self.entry_size] = new_blob
        self._write_bucket_page(page_id, count + 1, nxt,
                                bytes(body[:(count + 1) * self.entry_size]))
        self.buffer.unpin(self.file_id, page_id, dirty=False)
        # read_bucket_page pinned; write_page also pinned; both need unpinning
        # — but write_page already unpinned its own pin via the BM contract.
        # The unpin above releases the read_bucket_page pin.

    def lookup(self, key) -> Optional[Tuple[int, int]]:
        """Return (data_page_id, slot_id) for `key`, or None if absent."""
        bucket = _hash_key(key)
        head = self._get_bucket_head(bucket)
        cur = head
        while cur != -1:
            page, count, nxt = self._read_bucket_page(cur)
            try:
                for i in range(count):
                    off = _BHDR_SIZE + i * self.entry_size
                    blob = page[off:off + self.entry_size]
                    ekey, dp, sl = _unpack_entry(self.schema, blob)
                    if ekey == key:
                        return dp, sl
            finally:
                self.buffer.unpin(self.file_id, cur, dirty=False)
            cur = nxt
        return None

    def delete(self, key) -> bool:
        """Remove the entry for `key`. Compacts the page (moves last entry
        into the freed slot)."""
        bucket = _hash_key(key)
        head = self._get_bucket_head(bucket)
        cur = head
        while cur != -1:
            page, count, nxt = self._read_bucket_page(cur)
            self.buffer.unpin(self.file_id, cur, dirty=False)
            found_i = -1
            for i in range(count):
                off = _BHDR_SIZE + i * self.entry_size
                blob = page[off:off + self.entry_size]
                ekey, _, _ = _unpack_entry(self.schema, blob)
                if ekey == key:
                    found_i = i
                    break
            if found_i != -1:
                # Build new body: remove entry at found_i; move last entry
                # into its slot (compact).
                body = bytearray((count - 1) * self.entry_size)
                # copy entries [0..found_i)
                if found_i > 0:
                    body[:found_i * self.entry_size] = page[
                        _BHDR_SIZE:_BHDR_SIZE + found_i * self.entry_size
                    ]
                # move last into the gap, copy the rest unchanged
                if found_i < count - 1:
                    last_off = _BHDR_SIZE + (count - 1) * self.entry_size
                    last_blob = page[last_off:last_off + self.entry_size]
                    # entries between found_i+1 .. count-2 stay where they are
                    middle_len = (count - 1 - found_i - 1) * self.entry_size
                    if middle_len > 0:
                        body[(found_i + 1) * self.entry_size:
                             (found_i + 1) * self.entry_size + middle_len] = (
                            page[_BHDR_SIZE + (found_i + 1) * self.entry_size:
                                 _BHDR_SIZE + (found_i + 1) * self.entry_size
                                 + middle_len]
                        )
                    # last entry now at position found_i
                    body[found_i * self.entry_size:
                         (found_i + 1) * self.entry_size] = last_blob
                self._write_bucket_page(cur, count - 1, nxt, bytes(body))
                return True
            cur = nxt
        return False

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def rebuild_from_data(self, iter_records_with_loc) -> None:
        """Rebuild the entire index from an iterable yielding
        (key, data_page_id, slot_id) tuples. Used at engine startup when
        a .hidx file is missing or stale."""
        self.build()
        for key, dp, sl in iter_records_with_loc:
            self.insert(key, dp, sl)

    def reset_counter(self) -> None:
        self.nodes_visited = 0
