"""Layer 3 - File & Index Manager.

Understands records, relations (types), pages, and indexes. ALWAYS goes
through the Buffer Manager for page access -- never calls DiskSpaceManager
directly for data.

Step 4b implements `heap_scan` (the baseline strategy). Steps 4c and 4d
will plug in `hash_index` and `bplus_tree` without changing this class's
public surface.

Public surface (used by the Query Processor)
--------------------------------------------
    create_type(tokens)               -> OpResult
    insert_record(type_name, values)  -> OpResult
    delete_record(type_name, pk_value)-> OpResult
    search_by_pk(type_name, pk_value) -> RecordResult
    range_search(type_name, field, lo, hi) -> RecordResult

All five always return a Result object. Errors (duplicate primary keys,
missing types, type mismatch on values, etc.) come back as success=False
or status=FAILURE -- never as raised exceptions. The Query Processor will
log those as `failure` in log.csv.
"""

from typing import List, Optional

from results import RecordResult, OpResult, SUCCESS, FAILURE, ERROR

from .schema import (
    Schema, validate_record, schema_from_create_type_tokens,
)
from .catalog import Catalog
from . import page as P


class FileIndexManager:
    """Layer 3."""

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def __init__(self, config: dict, buffer):
        self.config = config
        self.buffer = buffer
        # Convenience: the DiskSpaceManager sits below the buffer. We use it
        # ONLY for non-page metadata (file_exists, create_file, num_pages).
        # Real page traffic still goes through the buffer.
        self.disk = buffer.disk
        self.page_size = config["page_size"]
        self.max_records_per_page = config["max_records_per_page"]
        self.index_strategy = config["index_strategy"]

        # System catalog. bootstrap() will load existing types or create an
        # empty catalog file on first run.
        self.catalog = Catalog(buffer=buffer, disk=self.disk,
                               page_size=self.page_size)
        self.catalog.bootstrap()

        # Cumulative counter for index-node traffic. Heap_scan never bumps
        # it (spec: "0 for heap_scan"). The hash / B+ implementations in
        # steps 4c/4d will. Reset via stats_reset().
        self.index_nodes_visited = 0

        # Per-type index objects, keyed by type name. Populated on
        # create_type (new types) and on bootstrap (existing types whose
        # .hidx/.bidx files are already on disk). Stays empty for heap_scan.
        self._indexes = {}
        if self.index_strategy != "heap_scan":
            self._bootstrap_indexes_for_existing_types()

    # ------------------------------------------------------------------
    # Index bootstrap (used when reopening with an indexed strategy)
    # ------------------------------------------------------------------
    def _bootstrap_indexes_for_existing_types(self) -> None:
        """For every type already in the catalog, attach the active index by
        rebuilding it from the data file. We ALWAYS rebuild on startup --
        not only when the index file is missing -- because a previous run
        under a different strategy (e.g. heap_scan, or the other index kind)
        may have mutated `<type>.dat` without touching this strategy's index
        file, leaving it stale and producing wrong answers."""
        for type_name in list(self.catalog.all_types()):
            schema = self.catalog.get_schema(type_name)
            # Defensive: if a type's data file is missing (orphaned catalog
            # entry from manual deletion or partial filesystem state), skip
            # so the engine can still start. Subsequent operations on this
            # type will surface as 'failure' via QueryProcessor's exception
            # handler instead of crashing bootstrap.
            if not self.disk.file_exists(schema.name):
                continue
            idx = self._make_index(schema)
            if idx is None:
                continue
            # Drop any stale index file from a previous run, then rebuild
            # from the data file. Cost is O(N) at startup; negligible for
            # our workloads and guarantees correctness across strategy
            # switches.
            if self.disk.file_exists(idx.file_id):
                self.disk.delete_file(idx.file_id)
            idx.rebuild_from_data(self._iter_locations(schema))
            self._indexes[type_name] = idx

    def _make_index(self, schema):
        """Construct the right index object for the current strategy."""
        if self.index_strategy == "hash_index":
            from .hash_index import HashIndex
            return HashIndex(schema, self.buffer, self.disk, self.page_size)
        if self.index_strategy == "bplus_tree":
            from .bplus_tree import BPlusTree
            return BPlusTree(schema, self.buffer, self.disk, self.page_size,
                             fanout=self.max_records_per_page)
        return None

    def _iter_locations(self, schema):
        """Yield (pk_value, page_id, slot_id) for every record of `schema`
        currently on disk. Used by rebuild_from_data."""
        pk_idx = schema.primary_key_index
        for page_id in range(self.disk.num_pages(schema.name)):
            _, page = self._read_data_page(schema.name, page_id)
            try:
                for slot, rec in P.iter_records(
                    page, schema, self.max_records_per_page
                ):
                    yield rec[pk_idx], page_id, slot
            finally:
                self._unpin(schema.name, page_id, dirty=False)

    # ------------------------------------------------------------------
    # Page-level helpers (heap-scan primitives)
    # ------------------------------------------------------------------
    def _data_file(self, type_name: str) -> str:
        return type_name  # DSM appends '.dat'

    def _ensure_data_file(self, type_name: str) -> None:
        """Make sure the .dat file exists. Called when a type is created."""
        fid = self._data_file(type_name)
        if not self.disk.file_exists(fid):
            self.disk.create_file(fid)

    def _read_data_page(self, type_name: str, page_id: int):
        """Pin and fetch a data page through the buffer. Returns (BufferResult,
        page_bytes). Caller MUST eventually unpin via _unpin."""
        bres = self.buffer.get_page(self._data_file(type_name), page_id)
        return bres, bres.page.data

    def _unpin(self, type_name: str, page_id: int, dirty: bool) -> None:
        self.buffer.unpin(self._data_file(type_name), page_id, dirty=dirty)

    def _write_data_page(self, type_name: str, page_id: int,
                         new_bytes: bytes) -> None:
        """Overwrite an entire data page through the buffer (marks dirty)."""
        self.buffer.write_page(self._data_file(type_name), page_id, new_bytes)

    def _alloc_data_page(self, type_name: str) -> int:
        """Allocate a new data page and seed it with an empty header on disk.

        Returns the new logical page id. We immediately overwrite the page
        with an empty header so a later read sees a consistent state.
        """
        alloc = self.disk.allocate_page(self._data_file(type_name))
        empty = P.empty_page(alloc.page_id, self.page_size)
        # Persist the empty header via the buffer so the dirty bit / cache
        # behave consistently with everything else.
        self._write_data_page(type_name, alloc.page_id, empty)
        return alloc.page_id

    def _data_page_count(self, type_name: str) -> int:
        return self.disk.num_pages(self._data_file(type_name))

    # ------------------------------------------------------------------
    # CREATE TYPE
    # ------------------------------------------------------------------
    def create_type(self, tokens: List[str]) -> OpResult:
        """`tokens` is the part of the input after the 'create type' words."""
        try:
            schema = schema_from_create_type_tokens(tokens)
        except ValueError as e:
            return OpResult(success=False, status=FAILURE, error_msg=str(e))

        if self.catalog.has_type(schema.name):
            return OpResult(
                success=False, status=FAILURE,
                error_msg=f"type already exists: {schema.name!r}",
            )

        # Sanity: does max_records_per_page fit?
        try:
            P.capacity_check(self.page_size, self.max_records_per_page,
                             schema.record_size)
        except ValueError as e:
            return OpResult(success=False, status=FAILURE, error_msg=str(e))

        # Persist the new type in the catalog and create its empty data file.
        self.catalog.add_type(schema)
        self._ensure_data_file(schema.name)
        # Build the index for this type (if the active strategy uses one).
        idx = self._make_index(schema)
        if idx is not None:
            idx.build()
            self._indexes[schema.name] = idx
        return OpResult(success=True)

    # ------------------------------------------------------------------
    # INSERT RECORD
    # ------------------------------------------------------------------
    def insert_record(self, type_name: str, values: List) -> OpResult:
        if not self.catalog.has_type(type_name):
            return OpResult(
                success=False, status=FAILURE,
                error_msg=f"no such type: {type_name!r}",
            )
        schema = self.catalog.get_schema(type_name)

        # Coerce numeric strings to int per the schema. The parser hands us
        # everything as strings; we convert here so validate_record sees the
        # right types. validate_record itself accepts numeric strings, but
        # downstream comparisons (e.g. duplicate-key check) want real ints.
        try:
            values = self._coerce_values(schema, values)
            validate_record(schema, values)
        except ValueError as e:
            return OpResult(success=False, status=FAILURE, error_msg=str(e))

        pk_idx = schema.primary_key_index
        new_pk = values[pk_idx]

        # Duplicate-PK check. Use the index if we have one; otherwise heap
        # scan. The index path is O(1) buckets touched (hash) or O(log n)
        # nodes (B+); the heap path is O(pages).
        pages_accessed = 0
        idx = self._indexes.get(type_name)
        if idx is not None:
            nodes_before = idx.nodes_visited
            existing = idx.lookup(new_pk)
            self.index_nodes_visited += idx.nodes_visited - nodes_before
            if existing is not None:
                return OpResult(
                    success=False, status=FAILURE,
                    pages_accessed=0,
                    index_nodes_visited=idx.nodes_visited - nodes_before,
                    error_msg=f"duplicate primary key: {new_pk!r}",
                )
        else:
            for page_id in range(self._data_page_count(type_name)):
                _, page = self._read_data_page(type_name, page_id)
                pages_accessed += 1
                found_dup = False
                try:
                    for _, rec in P.iter_records(
                        page, schema, self.max_records_per_page
                    ):
                        if rec[pk_idx] == new_pk:
                            found_dup = True
                            break
                finally:
                    self._unpin(type_name, page_id, dirty=False)
                if found_dup:
                    return OpResult(
                        success=False, status=FAILURE,
                        pages_accessed=pages_accessed,
                        error_msg=f"duplicate primary key: {new_pk!r}",
                    )

        # Find a page with a free slot, or allocate a new one.
        target_page_id: Optional[int] = None
        target_slot: Optional[int] = None
        for page_id in range(self._data_page_count(type_name)):
            _, page = self._read_data_page(type_name, page_id)
            pages_accessed += 1
            page_dirty = False
            try:
                _, _, bitmap = P.read_header(page)
                slot = P.first_free_slot(bitmap, self.max_records_per_page)
                if slot is not None:
                    new_page = P.write_slot(page, slot, schema, values)
                    self._write_data_page(type_name, page_id, new_page)
                    page_dirty = True
                    target_page_id, target_slot = page_id, slot
            finally:
                self._unpin(type_name, page_id, dirty=page_dirty)
            if target_page_id is not None:
                break

        if target_page_id is None:
            # No free slot anywhere -- allocate a new page.
            new_pid = self._alloc_data_page(type_name)
            pages_accessed += 1
            _, page = self._read_data_page(type_name, new_pid)
            try:
                new_page = P.write_slot(page, 0, schema, values)
                self._write_data_page(type_name, new_pid, new_page)
            finally:
                self._unpin(type_name, new_pid, dirty=True)
            target_page_id, target_slot = new_pid, 0

        # Maintain the index if one is active.
        nodes = 0
        if idx is not None:
            nb = idx.nodes_visited
            idx.insert(new_pk, target_page_id, target_slot)
            nodes = idx.nodes_visited - nb
            self.index_nodes_visited += nodes

        return OpResult(success=True, pages_accessed=pages_accessed,
                        index_nodes_visited=nodes)

    # ------------------------------------------------------------------
    # DELETE RECORD
    # ------------------------------------------------------------------
    def delete_record(self, type_name: str, pk_value) -> OpResult:
        if not self.catalog.has_type(type_name):
            return OpResult(
                success=False, status=FAILURE,
                error_msg=f"no such type: {type_name!r}",
            )
        schema = self.catalog.get_schema(type_name)
        try:
            pk_value = self._coerce_one(schema.primary_key_field[1], pk_value)
        except ValueError as e:
            return OpResult(success=False, status=FAILURE, error_msg=str(e))

        pk_idx = schema.primary_key_index
        pages_accessed = 0
        idx = self._indexes.get(type_name)

        if idx is not None:
            # Index-based: lookup location, fetch that page only, delete.
            nb = idx.nodes_visited
            loc = idx.lookup(pk_value)
            self.index_nodes_visited += idx.nodes_visited - nb
            if loc is None:
                return OpResult(
                    success=False, status=FAILURE,
                    index_nodes_visited=idx.nodes_visited - nb,
                    error_msg=f"record not found: pk={pk_value!r}",
                )
            page_id, slot = loc
            _, page = self._read_data_page(type_name, page_id)
            pages_accessed = 1
            try:
                new_page = P.delete_slot(page, slot, schema.record_size)
                self._write_data_page(type_name, page_id, new_page)
            finally:
                self._unpin(type_name, page_id, dirty=True)
            # remove from index too
            nb2 = idx.nodes_visited
            idx.delete(pk_value)
            self.index_nodes_visited += idx.nodes_visited - nb2
            return OpResult(
                success=True,
                pages_accessed=pages_accessed,
                index_nodes_visited=idx.nodes_visited - nb,
            )

        # Fallback: heap scan
        for page_id in range(self._data_page_count(type_name)):
            _, page = self._read_data_page(type_name, page_id)
            pages_accessed += 1
            page_dirty = False
            deleted = False
            try:
                found_slot = None
                for slot, rec in P.iter_records(
                    page, schema, self.max_records_per_page
                ):
                    if rec[pk_idx] == pk_value:
                        found_slot = slot
                        break
                if found_slot is not None:
                    new_page = P.delete_slot(page, found_slot, schema.record_size)
                    self._write_data_page(type_name, page_id, new_page)
                    page_dirty = True
                    deleted = True
            finally:
                self._unpin(type_name, page_id, dirty=page_dirty)
            if deleted:
                return OpResult(success=True, pages_accessed=pages_accessed)

        return OpResult(
            success=False, status=FAILURE,
            pages_accessed=pages_accessed,
            error_msg=f"record not found: pk={pk_value!r}",
        )

    # ------------------------------------------------------------------
    # SEARCH BY PRIMARY KEY  (equality)
    # ------------------------------------------------------------------
    def search_by_pk(self, type_name: str, pk_value) -> RecordResult:
        if not self.catalog.has_type(type_name):
            return RecordResult(
                status=FAILURE,
                error_msg=f"no such type: {type_name!r}",
            )
        schema = self.catalog.get_schema(type_name)
        try:
            pk_value = self._coerce_one(schema.primary_key_field[1], pk_value)
        except ValueError as e:
            return RecordResult(status=FAILURE, error_msg=str(e))

        pk_idx = schema.primary_key_index
        idx = self._indexes.get(type_name)

        if idx is not None:
            # Equality lookup via the index: 1 data page, ~1 index page.
            nb = idx.nodes_visited
            loc = idx.lookup(pk_value)
            visited = idx.nodes_visited - nb
            self.index_nodes_visited += visited
            if loc is None:
                return RecordResult(
                    status=FAILURE,
                    pages_accessed=0,
                    records_scanned=0,
                    index_nodes_visited=visited,
                    error_msg=f"record not found: pk={pk_value!r}",
                )
            page_id, slot = loc
            _, page = self._read_data_page(type_name, page_id)
            try:
                rec = P.read_slot(page, slot, schema)
                return RecordResult(
                    records=[rec],
                    pages_accessed=1,
                    records_scanned=1,
                    index_nodes_visited=visited,
                    status=SUCCESS,
                )
            finally:
                self._unpin(type_name, page_id, dirty=False)

        # Fallback: heap scan
        pages_accessed = 0
        records_examined = 0
        for page_id in range(self._data_page_count(type_name)):
            _, page = self._read_data_page(type_name, page_id)
            pages_accessed += 1
            found_rec = None
            try:
                for _, rec in P.iter_records(
                    page, schema, self.max_records_per_page
                ):
                    records_examined += 1
                    if rec[pk_idx] == pk_value:
                        found_rec = rec
                        break
            finally:
                self._unpin(type_name, page_id, dirty=False)
            if found_rec is not None:
                return RecordResult(
                    records=[found_rec],
                    pages_accessed=pages_accessed,
                    records_scanned=records_examined,
                    status=SUCCESS,
                )

        return RecordResult(
            pages_accessed=pages_accessed,
            records_scanned=records_examined,
            status=FAILURE,
            error_msg=f"record not found: pk={pk_value!r}",
        )

    # ------------------------------------------------------------------
    # RANGE SEARCH on an integer field
    # ------------------------------------------------------------------
    def range_search(self, type_name: str, field_name: str,
                     lo, hi) -> RecordResult:
        if not self.catalog.has_type(type_name):
            return RecordResult(
                status=FAILURE,
                error_msg=f"no such type: {type_name!r}",
            )
        schema = self.catalog.get_schema(type_name)
        try:
            field_idx = schema.field_index(field_name)
        except KeyError:
            return RecordResult(
                status=FAILURE,
                error_msg=f"no such field: {field_name!r}",
            )
        # Spec section 7.2 + 7.4: range search only on int fields.
        if schema.fields[field_idx][1] != "int":
            return RecordResult(
                status=FAILURE,
                error_msg=(
                    f"range search requires an int field; "
                    f"{field_name!r} is {schema.fields[field_idx][1]}"
                ),
            )
        try:
            lo, hi = int(lo), int(hi)
        except ValueError as e:
            return RecordResult(status=FAILURE, error_msg=str(e))

        # Fast path: if the active index is a B+ tree AND the queried field
        # is the primary key, use the tree (sorted leaf chain) to gather
        # only the relevant data pages. Otherwise fall back to heap_scan.
        idx = self._indexes.get(type_name)
        use_btree = (
            self.index_strategy == "bplus_tree"
            and idx is not None
            and field_idx == schema.primary_key_index
            and schema.primary_key_field[1] == "int"
        )

        if use_btree:
            nb = idx.nodes_visited
            locs = idx.range_search(lo, hi)
            visited = idx.nodes_visited - nb
            self.index_nodes_visited += visited
            out: List[List] = []
            seen_pages = set()
            for page_id, slot in locs:
                _, page = self._read_data_page(type_name, page_id)
                try:
                    rec = P.read_slot(page, slot, schema)
                    out.append(rec)
                    seen_pages.add(page_id)
                finally:
                    self._unpin(type_name, page_id, dirty=False)
            return RecordResult(
                records=out,
                pages_accessed=len(seen_pages),
                records_scanned=len(out),
                index_nodes_visited=visited,
                status=SUCCESS,
            )

        # Fallback: heap_scan (used by heap_scan and hash_index strategies,
        # and by bplus_tree when the queried field isn't the PK).
        out = []
        pages_accessed = 0
        records_examined = 0
        for page_id in range(self._data_page_count(type_name)):
            _, page = self._read_data_page(type_name, page_id)
            pages_accessed += 1
            try:
                for _, rec in P.iter_records(
                    page, schema, self.max_records_per_page
                ):
                    records_examined += 1
                    if lo <= rec[field_idx] <= hi:
                        out.append(rec)
            finally:
                self._unpin(type_name, page_id, dirty=False)

        return RecordResult(
            records=out,
            pages_accessed=pages_accessed,
            records_scanned=records_examined,
            status=SUCCESS,
        )

    # ------------------------------------------------------------------
    # Misc helpers / stats
    # ------------------------------------------------------------------
    def stats_reset(self) -> None:
        self.index_nodes_visited = 0

    # ------------------------------------------------------------------
    # Internal: type coercion for parser-supplied string values
    # ------------------------------------------------------------------
    @staticmethod
    def _coerce_values(schema: Schema, values: List) -> List:
        out = []
        for (_, ftype), v in zip(schema.fields, values):
            out.append(FileIndexManager._coerce_one(ftype, v))
        return out

    @staticmethod
    def _coerce_one(ftype: str, v):
        if ftype == "int":
            if isinstance(v, int):
                return v
            if isinstance(v, str):
                try:
                    return int(v)
                except ValueError:
                    raise ValueError(f"expected int, got {v!r}")
            raise ValueError(f"expected int, got {type(v).__name__}")
        # str
        if not isinstance(v, str):
            return str(v)
        return v
