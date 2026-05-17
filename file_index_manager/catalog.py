"""Step 4b -- System catalog.

Persists the metadata for every registered type in a normal DSM-managed
file, `catalog.dat`, accessed strictly through the Buffer Manager.

Why a separate module?
----------------------
The catalog is the only piece of state the FileIndexManager has to recover
across runs. Keeping it in its own class makes that recovery path obvious
and keeps the main FileIndexManager focused on records and indexes.

Catalog file layout
-------------------
We use a single catalog page (logical page 0 of catalog.dat). 4096 bytes is
ample: each schema entry is ~218 B, and we'd need ~18 types to fill a page
even before any optimization. If we ever exceed that we can add overflow
pages, but it's not needed for the project's workloads.

Page 0 layout:
    bytes 0..3 :  num_types   (uint32 LE)
    bytes 4..  :  N schema entries packed back-to-back via pack_schema()

When the engine is restarted, `load()` reads page 0 and rebuilds the in-memory
dict {type_name -> Schema}. No state needs to live anywhere else.
"""

import struct
from typing import Dict, Iterable

from .schema import Schema, pack_schema, unpack_schema

CATALOG_FILE = "catalog"            # DSM appends '.dat' for us
_CATALOG_PAGE = 0                   # we use the first logical page
_NUM_TYPES_FMT = "<I"
_NUM_TYPES_SIZE = struct.calcsize(_NUM_TYPES_FMT)   # 4 bytes


class Catalog:
    """In-memory view of the system catalog, kept in sync with disk."""

    def __init__(self, buffer, disk, page_size: int):
        self.buffer = buffer
        self.disk = disk
        self.page_size = page_size
        self._types: Dict[str, Schema] = {}

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------
    def bootstrap(self) -> None:
        """Create catalog.dat on first run, or load existing types from it.

        Idempotent: calling on a fresh engine creates an empty page; calling
        when catalog.dat already exists reads and decodes it.
        """
        if not self.disk.file_exists(CATALOG_FILE):
            # First boot: create the file and reserve a catalog page.
            self.disk.create_file(CATALOG_FILE)
            self.disk.allocate_page(CATALOG_FILE)
            # Write an empty header (num_types = 0).
            self._persist()
        else:
            self._load()

    def _load(self) -> None:
        """Read page 0 of catalog.dat and rebuild self._types."""
        bres = self.buffer.get_page(CATALOG_FILE, _CATALOG_PAGE)
        try:
            page = bres.page.data
            (num_types,) = struct.unpack_from(_NUM_TYPES_FMT, page, 0)
            cursor = _NUM_TYPES_SIZE
            self._types.clear()
            for _ in range(num_types):
                schema, used = unpack_schema(page, cursor)
                self._types[schema.name] = schema
                cursor += used
        finally:
            self.buffer.unpin(CATALOG_FILE, _CATALOG_PAGE, dirty=False)

    def _persist(self) -> None:
        """Write the current in-memory catalog back to page 0."""
        buf = bytearray(self.page_size)
        struct.pack_into(_NUM_TYPES_FMT, buf, 0, len(self._types))
        cursor = _NUM_TYPES_SIZE
        for schema in self._types.values():
            blob = pack_schema(schema)
            buf[cursor:cursor + len(blob)] = blob
            cursor += len(blob)
            if cursor > self.page_size:
                raise RuntimeError(
                    "catalog page overflow -- too many types for one page"
                )
        # write_page goes through the Buffer Manager so the dirty bit / cache
        # behave correctly; the underlying disk write will happen on eviction
        # or on flush(), exactly like a record write.
        self.buffer.write_page(CATALOG_FILE, _CATALOG_PAGE, bytes(buf))

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------
    def has_type(self, name: str) -> bool:
        return name in self._types

    def get_schema(self, name: str) -> Schema:
        return self._types[name]

    def add_type(self, schema: Schema) -> None:
        if schema.name in self._types:
            raise ValueError(f"type already exists: {schema.name!r}")
        self._types[schema.name] = schema
        self._persist()

    def all_types(self) -> Iterable[str]:
        return self._types.keys()
