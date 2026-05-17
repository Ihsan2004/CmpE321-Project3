"""Layer 2 - Buffer Manager.

In-memory page cache sitting between the File & Index Manager and the
Disk Space Manager. On every page request:
  - If the page is in the pool -> cache hit, no disk I/O.
  - Otherwise -> miss: pick a victim per replacement policy, write it back
    if dirty, evict it, then load the requested page from disk into its
    frame.

The active replacement policy is chosen by `config["replacement_policy"]`:
"LRU" evicts the least recently used page; "MRU" evicts the most recently
used. Both are implemented over a single OrderedDict so the data structure
itself doesn't care which policy is active -- only the eviction call differs.

Pin / unpin
-----------
While the File & Index Manager is operating on a page's bytes, that page is
"pinned" and cannot be evicted. The protocol is:

    bres = bm.get_page(file_id, page_id)   # pin == 1
    ... use / modify bres.page.data ...
    bm.unpin(file_id, page_id, dirty=True) # pin -> 0, marked dirty

A pin count (rather than a boolean) lets multiple users hold the same page
simultaneously; this never happens in the current upper layers but it costs
nothing and avoids a subtle class of bugs.

Statistics (spec 4.2)
---------------------
We expose the following plain counters:
    requests          -- every get_page call
    hits              -- requests served from pool
    misses            -- requests that required disk I/O
    evictions         -- victim frames evicted to make room
    dirty_writebacks  -- subset of evictions whose victim was dirty
"""

from collections import OrderedDict
from typing import Optional

from results import PageResult, BufferResult


class _Frame:
    """One slot in the buffer pool. Holds a single page plus bookkeeping."""

    __slots__ = ("file_id", "page_id", "data", "dirty", "pin_count")

    def __init__(self, file_id: str, page_id: int, data: bytes):
        self.file_id = file_id
        self.page_id = page_id
        self.data = data
        self.dirty = False
        self.pin_count = 0

    def key(self):
        return (self.file_id, self.page_id)


class BufferManager:
    """Caches pages in memory; delegates real I/O to DiskSpaceManager."""

    # ----- construction -------------------------------------------------
    def __init__(self, config: dict, disk):
        self.config = config
        self.disk = disk
        self.pool_size = config["buffer_pool_size"]
        self.policy = config["replacement_policy"].upper()
        if self.policy not in ("LRU", "MRU"):
            raise ValueError(f"unknown replacement_policy: {self.policy!r}")

        # frames: keyed by (file_id, page_id). OrderedDict gives us both
        # O(1) membership and O(1) move-to-end / pop-from-either-end, which
        # is exactly what LRU and MRU need.
        # Convention used everywhere in this class:
        #   - "end" (right side) = MOST recently used
        #   - "front" (left side) = LEAST recently used
        # LRU eviction pops from the front; MRU eviction pops from the end.
        self._frames: "OrderedDict[tuple, _Frame]" = OrderedDict()

        # Statistics (spec 4.2). All cumulative; QueryProcessor resets them
        # via stats_reset() when the user types `stats reset`.
        self.requests = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.dirty_writebacks = 0

    # ----- private helpers ---------------------------------------------
    def _touch(self, key) -> None:
        """Mark `key` as most-recently-used by moving it to the end."""
        self._frames.move_to_end(key, last=True)

    def _pick_victim(self) -> Optional[tuple]:
        """Choose a frame to evict according to the active policy.

        Skips pinned frames. Returns the victim's key, or None if every
        frame is pinned (which would be a real bug, not normal operation).
        """
        if self.policy == "LRU":
            # walk from oldest (front) to newest (end)
            iterator = iter(self._frames.items())
        else:  # MRU
            # walk from newest (end) to oldest (front)
            iterator = iter(reversed(list(self._frames.items())))
        for key, frame in iterator:
            if frame.pin_count == 0:
                return key
        return None  # every frame is pinned -- caller decides what to do

    def _evict_one(self) -> tuple[Optional[int], bool]:
        """Evict one frame. Returns (evicted_page_id, was_dirty_writeback)."""
        victim_key = self._pick_victim()
        if victim_key is None:
            # No unpinned frame -> the pool is overcommitted. We could
            # raise, but reporting cleanly via BufferResult is friendlier.
            raise RuntimeError(
                "buffer pool is fully pinned; cannot evict. "
                "An upper-layer caller forgot to unpin a page."
            )
        victim = self._frames.pop(victim_key)
        self.evictions += 1
        wb = False
        if victim.dirty:
            # Write the page back through the DSM (real disk I/O).
            self.disk.write_page(victim.file_id, victim.page_id, victim.data)
            self.dirty_writebacks += 1
            wb = True
        return victim.page_id, wb

    # ----- public API ---------------------------------------------------
    def get_page(self, file_id: str, page_id: int) -> BufferResult:
        """Fetch a page (pinned) from the pool, loading it from disk if
        necessary. Always returns a BufferResult."""
        self.requests += 1
        key = (file_id, page_id)

        if key in self._frames:
            # Cache hit.
            frame = self._frames[key]
            self._touch(key)
            frame.pin_count += 1
            self.hits += 1
            page = PageResult(
                data=frame.data,
                page_id=page_id,
                file_id=file_id,
                io_performed=False,   # served from memory
            )
            return BufferResult(page=page, cache_hit=True)

        # Cache miss.
        self.misses += 1
        evicted_page_id = None
        dirty_writeback = False
        if len(self._frames) >= self.pool_size:
            evicted_page_id, dirty_writeback = self._evict_one()

        # Bring the page in from disk. DSM returns a PageResult with
        # io_performed=True, which we re-use verbatim.
        disk_pr = self.disk.read_page(file_id, page_id)
        frame = _Frame(file_id, page_id, disk_pr.data)
        frame.pin_count = 1
        self._frames[key] = frame   # inserted at the end == MRU
        return BufferResult(
            page=disk_pr,
            cache_hit=False,
            evicted_page_id=evicted_page_id,
            dirty_writeback=dirty_writeback,
        )

    def unpin(self, file_id: str, page_id: int, dirty: bool = False) -> None:
        """Release a previously pinned page. Pass dirty=True if the bytes
        have been modified since get_page() returned them."""
        key = (file_id, page_id)
        if key not in self._frames:
            # Trying to unpin a page that isn't in the pool would mean
            # we lost track somewhere -- surface it loudly.
            raise KeyError(f"unpin: page {key} is not in the buffer pool")
        frame = self._frames[key]
        if frame.pin_count <= 0:
            raise RuntimeError(f"unpin: page {key} is not pinned")
        frame.pin_count -= 1
        if dirty:
            frame.dirty = True

    def write_page(self, file_id: str, page_id: int, data: bytes) -> BufferResult:
        """Replace a page's bytes (and mark it dirty) in one shot.

        Useful when an upper layer rebuilds a whole page rather than mutating
        the bytes in place. The page is pinned just for the duration of this
        call. This goes through the pool, so it still benefits from caching
        and the dirty bit -- no disk I/O happens here unless an eviction is
        required to make room.
        """
        bres = self.get_page(file_id, page_id)
        # overwrite the cached bytes
        key = (file_id, page_id)
        frame = self._frames[key]
        if len(data) != len(frame.data):
            raise ValueError(
                f"write_page: data length {len(data)} != page size "
                f"{len(frame.data)}"
            )
        frame.data = data
        frame.dirty = True
        self.unpin(file_id, page_id, dirty=True)
        # Return a BufferResult so the caller still sees eviction info.
        return bres

    # ----- DSM metadata / allocation wrappers -------------------------
    # Layer 3 still needs file lifecycle and page-count services, but it
    # reaches them through this layer so every inter-layer call returns a
    # Result object and the DiskSpaceManager remains behind the buffer.
    def file_exists(self, file_id: str):
        return self.disk.file_exists(file_id)

    def create_file(self, file_id: str):
        return self.disk.create_file(file_id)

    def delete_file(self, file_id: str):
        return self.disk.delete_file(file_id)

    def allocate_page(self, file_id: str):
        return self.disk.allocate_page(file_id)

    def deallocate_page(self, file_id: str, page_id: int):
        return self.disk.deallocate_page(file_id, page_id)

    def num_pages(self, file_id: str):
        return self.disk.num_pages(file_id)

    def flush(self) -> None:
        """Write every dirty page back to disk and clear dirty flags.

        Called by archive.py at end-of-run, and a useful primitive for
        tests / explicit checkpoints.
        """
        # Iterate over a snapshot because we don't reorder during flush.
        for frame in list(self._frames.values()):
            if frame.dirty:
                self.disk.write_page(frame.file_id, frame.page_id, frame.data)
                frame.dirty = False
                self.dirty_writebacks += 1

    def stats_reset(self) -> None:
        """Zero the buffer counters (spec: `stats reset` command)."""
        self.requests = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.dirty_writebacks = 0

    # ----- introspection (used by tests and the Query Processor) -------
    def hit_rate(self) -> float:
        """Hit rate as a float in [0, 1]. Returns 0 when no requests yet."""
        return self.hits / self.requests if self.requests else 0.0

    def __len__(self) -> int:
        return len(self._frames)

    def __contains__(self, key) -> bool:
        return key in self._frames
