"""
results.py - Inter-layer communication objects (spec section 5).

THE most important architectural rule of this project: every time one layer
calls another, the return value MUST be one of these Result objects -- never
a raw `bytes`, bare `int`, or plain list.

Each Result carries three things:
  1. the data itself      (page bytes, record values, ...)
  2. metadata             (was it a cache hit? how many I/Os? pages scanned?)
  3. a status             (success / failure / error + optional message)

Because all inter-layer traffic flows through these objects, each layer stays
a true black box: we can change a layer's internals without touching the
others, as long as it still hands back the same Result type.

Design notes:
  - We deliberately do NOT use dataclass inheritance. A shared base with
    default fields makes subclass field ordering painful, so instead every
    Result simply repeats the `status` / `error_msg` pair. Small, explicit,
    easy to explain.
  - `status` is one of the string constants below.
  - A "record" (in RecordResult.records) is, for now, just "whatever the File
    & Index Manager hands back" -- we pin down its exact shape in Step 4.
"""

from dataclasses import dataclass, field
from typing import Optional, Any, List


# --------------------------------------------------------------------------
# Status constants -- used by every Result's `status` field.
# --------------------------------------------------------------------------
SUCCESS = "success"   # operation completed and produced a result
FAILURE = "failure"   # operation ran fine but found nothing / was rejected
                      # (e.g. search for a missing key, duplicate primary key)
ERROR = "error"       # something genuinely went wrong (bad input, etc.)


# ==========================================================================
# Layer 1 -- DiskSpaceManager results
# ==========================================================================

@dataclass
class PageResult:
    """Returned by DiskSpaceManager (and re-used by BufferManager) when a
    single page is fetched.

    `io_performed` is True when a real disk read happened. The DiskSpaceManager
    always sets it True; the BufferManager will set it False on a cache hit.
    """
    data: bytes                       # the raw page bytes (page_size long)
    page_id: int                      # which page within the file
    file_id: str                      # which relation/index file it came from
    io_performed: bool = True         # did an actual disk read occur?
    status: str = SUCCESS
    error_msg: Optional[str] = None


@dataclass
class WriteResult:
    """Returned by DiskSpaceManager when a page is written to disk.

    Carries both the previous and the new page content -- this is what the
    `log_write` stub will eventually consume, and it lets upper layers reason
    about a write without re-reading the page.
    """
    success: bool
    file_id: str
    page_id: int
    old_data: bytes = b""             # page content before this write
    new_data: bytes = b""             # what was actually written
    status: str = SUCCESS
    error_msg: Optional[str] = None


@dataclass
class AllocResult:
    """Returned by DiskSpaceManager when a new page is allocated in a file."""
    success: bool
    file_id: str
    page_id: int                      # id of the freshly allocated page
    status: str = SUCCESS
    error_msg: Optional[str] = None


# ==========================================================================
# Layer 2 -- BufferManager results
# ==========================================================================

@dataclass
class BufferResult:
    """Returned by BufferManager for page fetches and page writes.

    On a fetch, `page` holds the requested page. On a pure write/mark-dirty
    call, `page` may be None. `cache_hit` is True when the page was already in
    the pool (no disk I/O). Eviction info is exposed for transparency even
    though the BufferManager also keeps its own cumulative counters.
    """
    page: Optional[PageResult]              # the page (None for pure writes)
    cache_hit: bool                         # True => served from pool, no I/O
    evicted_page_id: Optional[int] = None   # None if nothing was evicted
    dirty_writeback: bool = False           # True if evicted page was dirty
    status: str = SUCCESS
    error_msg: Optional[str] = None


# ==========================================================================
# Layer 3 -- FileIndexManager results
# ==========================================================================

@dataclass
class RecordResult:
    """Returned by FileIndexManager for search / range_search / scan.

    `records` is the list of matching records (empty on a miss).
    `pages_accessed` counts data pages touched while answering the query.
    `index_nodes_visited` is 0 for heap_scan, > 0 for hash_index / bplus_tree.
    `status` is SUCCESS when at least one record matched, FAILURE otherwise.
    """
    records: List[Any] = field(default_factory=list)
    pages_accessed: int = 0
    records_scanned: int = 0          # actual records iterated by this query
    index_nodes_visited: int = 0
    status: str = SUCCESS
    error_msg: Optional[str] = None


@dataclass
class OpResult:
    """Returned by FileIndexManager for create type / insert / delete.

    These operations don't return rows -- just whether they worked, plus the
    I/O-ish metadata so the Query Processor can report per-operation stats.
    A rejected operation (duplicate primary key, missing type, ...) comes back
    with success=False and status=FAILURE, NOT as a crash.
    """
    success: bool
    pages_accessed: int = 0
    index_nodes_visited: int = 0
    status: str = SUCCESS
    error_msg: Optional[str] = None


# ==========================================================================
# Quick self-check when run directly: python3 results.py
# ==========================================================================
if __name__ == "__main__":
    demo = [
        PageResult(data=b"\x00" * 16, page_id=0, file_id="house"),
        WriteResult(success=True, file_id="house", page_id=0,
                    old_data=b"", new_data=b"\x00" * 16),
        AllocResult(success=True, file_id="house", page_id=1),
        BufferResult(page=None, cache_hit=True),
        RecordResult(records=["Atreides", "Harkonnen"], pages_accessed=2),
        OpResult(success=False, status=FAILURE, error_msg="duplicate key"),
    ]
    for r in demo:
        print(type(r).__name__, "->", r)
