"""Layer 1 - Disk Space Manager.

The ONLY component that performs real file I/O. It deals in raw fixed-size
pages and knows nothing about records, indexes, or queries.

Per-file layout on disk
-----------------------
Every relation/index file we manage starts with one DSM-owned header page
(physical page 0), followed by content pages:

    physical 0 : DSM file header  (num_pages, free list, ...)
    physical 1 : content page that upper layers see as logical page 0
    physical 2 : content page that upper layers see as logical page 1
    ...

Upper layers never know page 0 exists. They request logical pages starting
at 0, and we translate logical -> physical = logical + 1 internally. This
keeps the abstraction clean: free-space bookkeeping is a DSM secret.

File header page (physical page 0) format
-----------------------------------------
    bytes 0..3   : magic            "DSM1" (sanity check)
    bytes 4..7   : num_content_pgs  total content pages allocated (uint32 LE)
    bytes 8..11  : free_count       how many entries in the free list (uint32)
    bytes 12..   : free_list        free_count * uint32, each a LOGICAL page id

The free list lives in page 0 itself. With page_size = 4096 that gives room
for (4096 - 12) / 4 = 1021 free entries -- plenty for our workloads.

Free space tracking
-------------------
A free *list* of logical page ids. When upper layers ask to deallocate a page,
we push its id onto the list. `allocate_page` first pops from the list; only
if it is empty do we extend the file.

I/O counting
------------
Every `_read_physical_page` increments `read_count`. Every `_write_physical_page`
increments `write_count`. Reading/writing the file header counts too -- it's
real disk traffic. The counters are exposed as plain attributes for upper
layers to read and snapshot.

log_write stub
--------------
A callable invoked on every write (spec 4.1). Default is a no-op; we keep it
as an instance attribute so it can be swapped in tests or by upper layers.
"""

import os
import struct
from typing import Callable

from results import PageResult, WriteResult, AllocResult, OpResult, FAILURE


# --- File header (physical page 0) constants ------------------------------
_MAGIC = b"DSM1"
_HEADER_FIXED_FMT = "<4sII"       # magic, num_content_pgs, free_count
_HEADER_FIXED_SIZE = struct.calcsize(_HEADER_FIXED_FMT)   # 12 bytes
_FREE_ENTRY_FMT = "<I"            # one free-list entry: logical page id
_FREE_ENTRY_SIZE = struct.calcsize(_FREE_ENTRY_FMT)       # 4 bytes


class DiskSpaceManager:
    """Reads/writes fixed-size pages to/from per-relation binary files."""

    # ----- construction --------------------------------------------------
    def __init__(self, config: dict):
        self.config = config
        self.page_size = config["page_size"]

        # Files live next to archive.py. We resolve that once, here.
        # __file__ is .../disk_space_manager/disk_space_manager.py, so
        # going up two directories lands on the project root.
        self.data_dir = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )

        # I/O counters (cumulative for the run; QueryProcessor resets them
        # via stats_reset() when the user types `stats reset`).
        self.read_count = 0
        self.write_count = 0

        # Spec 4.1: a log_write stub must exist and must be called on every
        # write. For now it is a no-op; an upper layer can replace it.
        self.log_write: Callable[[WriteResult], None] = lambda wr: None

        # Cache of open file handles, keyed by file_id (relation name +
        # optional ".hidx"/".bidx" suffix). Opening once per file avoids OS overhead.
        self._handles = {}

    # ----- path / handle helpers ----------------------------------------
    def _path(self, file_id: str) -> str:
        """Map a logical file_id (e.g. 'house', 'house.bidx') to a real path."""
        # We append '.dat' when the caller did not specify an extension, so
        # 'house' -> 'house.dat' but 'house.bidx' stays as 'house.bidx'.
        name = file_id if "." in file_id else file_id + ".dat"
        return os.path.join(self.data_dir, name)

    def _open(self, file_id: str):
        """Open (or reuse) a binary read/write handle for `file_id`."""
        if file_id in self._handles:
            return self._handles[file_id]
        path = self._path(file_id)
        # 'r+b' requires the file to exist; create_file() handles creation.
        f = open(path, "r+b")
        self._handles[file_id] = f
        return f

    # ----- physical page I/O (private; counted) -------------------------
    def _read_physical_page(self, file_id: str, phys_page: int) -> bytes:
        """Read one physical page. Counts as one disk read."""
        f = self._open(file_id)
        f.seek(phys_page * self.page_size)
        data = f.read(self.page_size)
        if len(data) < self.page_size:
            # Page wasn't fully written yet (shouldn't happen in normal use).
            # Pad with zeros so upper layers get a consistent page_size buffer.
            data = data + b"\x00" * (self.page_size - len(data))
        self.read_count += 1
        return data

    def _write_physical_page(self, file_id: str, phys_page: int,
                             data: bytes, result_page_id: int = None) -> bytes:
        """Write one physical page. Counts as one disk write.

        Returns the *previous* contents of the page (or zero bytes if the page
        is brand new) so the caller can fill a WriteResult with old_data.
        """
        if len(data) != self.page_size:
            raise ValueError(
                f"page write must be exactly {self.page_size} bytes, "
                f"got {len(data)}"
            )
        f = self._open(file_id)
        # Capture the old content first. If the page does not yet exist on
        # disk (we are extending the file), old_data is all zeros.
        f.seek(0, os.SEEK_END)
        end = f.tell()
        offset = phys_page * self.page_size
        if offset < end:
            f.seek(offset)
            old = f.read(self.page_size)
            if len(old) < self.page_size:
                old = old + b"\x00" * (self.page_size - len(old))
        else:
            old = b"\x00" * self.page_size
        f.seek(offset)
        f.write(data)
        f.flush()
        self.write_count += 1
        wr = WriteResult(
            success=True,
            file_id=file_id,
            page_id=phys_page if result_page_id is None else result_page_id,
            old_data=old,
            new_data=data,
        )
        self.log_write(wr)
        return old

    # ----- file header (page 0) read/write ------------------------------
    def _read_header(self, file_id: str):
        """Return (num_content_pages, free_list) for `file_id`."""
        raw = self._read_physical_page(file_id, 0)
        magic, num_content, free_count = struct.unpack_from(
            _HEADER_FIXED_FMT, raw, 0
        )
        if magic != _MAGIC:
            raise RuntimeError(
                f"file {file_id!r} is not a DSM-managed file (bad magic)"
            )
        free_list = []
        for i in range(free_count):
            off = _HEADER_FIXED_SIZE + i * _FREE_ENTRY_SIZE
            (logical,) = struct.unpack_from(_FREE_ENTRY_FMT, raw, off)
            free_list.append(logical)
        return num_content, free_list

    def _write_header(self, file_id: str, num_content: int,
                      free_list) -> None:
        """Serialize and write the file header back to physical page 0."""
        # Max free entries that fit. With page_size=4096 this is 1021.
        cap = (self.page_size - _HEADER_FIXED_SIZE) // _FREE_ENTRY_SIZE
        if len(free_list) > cap:
            raise RuntimeError(
                f"free list overflow for {file_id!r}: "
                f"{len(free_list)} entries, capacity {cap}"
            )
        buf = bytearray(self.page_size)
        struct.pack_into(_HEADER_FIXED_FMT, buf, 0,
                         _MAGIC, num_content, len(free_list))
        for i, logical in enumerate(free_list):
            off = _HEADER_FIXED_SIZE + i * _FREE_ENTRY_SIZE
            struct.pack_into(_FREE_ENTRY_FMT, buf, off, logical)
        self._write_physical_page(file_id, 0, bytes(buf))

    # ----- public API ---------------------------------------------------
    def file_exists(self, file_id: str) -> OpResult:
        """Return whether the file already exists on disk."""
        return OpResult(success=True, value=os.path.exists(self._path(file_id)))

    def create_file(self, file_id: str) -> OpResult:
        """Create a new, empty DSM-managed file (writes the header page).

        If the file already exists, returns a failure Result.
        """
        path = self._path(file_id)
        if os.path.exists(path):
            return OpResult(
                success=False,
                status=FAILURE,
                error_msg=f"file already exists: {file_id!r}",
            )
        # touch then open r+b
        with open(path, "wb") as f:
            f.write(b"")
        self._handles[file_id] = open(path, "r+b")
        # initialize: 0 content pages, empty free list
        self._write_header(file_id, 0, [])
        return OpResult(success=True)

    def allocate_page(self, file_id: str) -> AllocResult:
        """Allocate a new logical page in `file_id`.

        Pops from the free list if possible; otherwise extends the file by
        writing a fresh zero-filled page at the end.
        """
        num_content, free_list = self._read_header(file_id)
        if free_list:
            logical = free_list.pop()
            # No need to zero the page; the caller owns its contents now.
            self._write_header(file_id, num_content, free_list)
        else:
            logical = num_content
            num_content += 1
            phys = logical + 1
            # Materialize the new page on disk as zero-filled.
            self._write_physical_page(
                file_id, phys, b"\x00" * self.page_size,
                result_page_id=logical,
            )
            self._write_header(file_id, num_content, free_list)
        return AllocResult(success=True, file_id=file_id, page_id=logical)

    def read_page(self, file_id: str, page_id: int) -> PageResult:
        """Read one logical page from `file_id`."""
        phys = page_id + 1
        data = self._read_physical_page(file_id, phys)
        return PageResult(
            data=data,
            page_id=page_id,
            file_id=file_id,
            io_performed=True,
        )

    def write_page(self, file_id: str, page_id: int, data: bytes) -> WriteResult:
        """Write one logical page to `file_id`. Fires log_write."""
        phys = page_id + 1
        old = self._write_physical_page(
            file_id, phys, data, result_page_id=page_id
        )
        wr = WriteResult(
            success=True,
            file_id=file_id,
            page_id=page_id,
            old_data=old,
            new_data=data,
        )
        return wr

    def deallocate_page(self, file_id: str, page_id: int) -> OpResult:
        """Return a logical page to the free list."""
        num_content, free_list = self._read_header(file_id)
        if page_id in free_list:
            return OpResult(success=True)  # already free; idempotent
        if page_id < 0 or page_id >= num_content:
            return OpResult(
                success=False,
                status=FAILURE,
                error_msg=f"page {page_id} out of range for {file_id!r}",
            )
        free_list.append(page_id)
        self._write_header(file_id, num_content, free_list)
        return OpResult(success=True)

    def num_pages(self, file_id: str) -> OpResult:
        """Return the number of LOGICAL content pages in `file_id`.

        Note: includes pages currently on the free list (they are 'allocated'
        in the file but available for reuse). Upper layers usually iterate
        0..num_pages-1 and skip empty ones via their own bitmap.
        """
        num_content, _ = self._read_header(file_id)
        return OpResult(success=True, value=num_content)

    def stats_reset(self) -> None:
        """Zero the I/O counters (spec: `stats reset` command)."""
        self.read_count = 0
        self.write_count = 0

    def close(self) -> None:
        """Close all open file handles. Safe to call multiple times."""
        for f in self._handles.values():
            try:
                f.close()
            except Exception:
                pass
        self._handles.clear()

    def delete_file(self, file_id: str) -> OpResult:
        """Remove a DSM-managed file from disk. Closes the handle if open.
        Idempotent: silently no-op if the file does not exist."""
        if file_id in self._handles:
            try:
                self._handles[file_id].close()
            except Exception:
                pass
            del self._handles[file_id]
        path = self._path(file_id)
        if os.path.exists(path):
            os.remove(path)
        return OpResult(success=True)
