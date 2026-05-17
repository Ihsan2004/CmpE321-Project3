"""Step 4d -- B+ tree index on primary key.

Supports both equality lookup AND range search (unlike hash_index). Built
when a type is created, maintained on every insert/delete, consulted on
every equality lookup and on every range query whose field is the PK.

On-disk structure (`<type>.bidx`)
---------------------------------
Logical page 0  : tree header
    bytes 0..3  : root_page_id (int32, -1 if tree is empty)
    bytes 4..7  : height (int32, mostly for debugging)
    bytes 8..   : reserved / zero-filled

Logical pages 1..N : node pages, one per node. Two node kinds, distinguished
by a type tag in the first header byte.

Common node header (16 bytes)
    byte  0     : kind (0 = internal, 1 = leaf)
    bytes 1..3  : padding (zero)
    bytes 4..7  : page_id
    bytes 8..11 : count       (= number of keys for internals,
                                 number of entries for leaves)
    bytes 12..15: aux         (next_leaf for leaves, unused for internals)

Internal node body
    n keys, n+1 child page ids, laid out as: c0, k0, c1, k1, ..., k(n-1), cn
    i.e. interleaved so an in-place binary search reads keys naturally.

    Each child id = int32 (4 B), each key = key_width bytes
    (4 B for int PK, 32 B for str PK).

Leaf node body
    n entries, each (key, data_page_id, slot_id) = key_width + 4 + 4 bytes.
    The `next_leaf` field in the header points to the right sibling for
    range scans; -1 marks the last leaf.

Fanout
------
We deliberately cap the per-node entry count at `max_records_per_page`
from the config (default 10). This matches the spec's data-page cap and,
more importantly, gives us a tree whose depth grows with N so range/equality
benefits over heap_scan are actually observable in the experiments.

What we count
-------------
Every node read OR write bumps `nodes_visited` on this index object. The
FileIndexManager aggregates that delta into its own counter.
"""

import struct
from typing import List, Optional, Tuple

from .schema import Schema, INT_WIDTH, STR_WIDTH

# ---- node kinds ---------------------------------------------------------
_KIND_INTERNAL = 0
_KIND_LEAF = 1

# ---- header layout (same for both kinds) --------------------------------
_NODE_HDR_FMT = "<B3xiii"     # kind, 3 pad, page_id, count, aux
_NODE_HDR_SIZE = struct.calcsize(_NODE_HDR_FMT)   # 16 bytes
assert _NODE_HDR_SIZE == 16

# ---- tree-file header (logical page 0) ----------------------------------
_TREE_HDR_FMT = "<ii"         # root_page_id, height
_TREE_HDR_SIZE = struct.calcsize(_TREE_HDR_FMT)   # 8 bytes


def _key_width(schema: Schema) -> int:
    _, pk_type = schema.primary_key_field
    return INT_WIDTH if pk_type == "int" else STR_WIDTH


def _pack_key(schema: Schema, key) -> bytes:
    _, pk_type = schema.primary_key_field
    if pk_type == "int":
        return struct.pack("<i", int(key))
    b = key.encode("ascii")
    if len(b) > STR_WIDTH:
        raise ValueError(f"key too long: {key!r}")
    return b + b"\x00" * (STR_WIDTH - len(b))


def _unpack_key(schema: Schema, blob: bytes):
    _, pk_type = schema.primary_key_field
    if pk_type == "int":
        return struct.unpack("<i", blob[:INT_WIDTH])[0]
    return blob[:STR_WIDTH].rstrip(b"\x00").decode("ascii")


class BPlusTree:
    """B+ tree on primary key. Public surface mirrors HashIndex's so the
    FileIndexManager can swap them transparently."""

    def __init__(self, schema: Schema, buffer, disk, page_size: int,
                 fanout: int):
        self.schema = schema
        self.buffer = buffer
        self.disk = disk
        self.page_size = page_size
        self.file_id = f"{schema.name}.bidx"
        self.kw = _key_width(schema)
        # Max keys per node. We use the same cap as max_records_per_page so
        # the tree actually has multiple levels in the project's workloads.
        self.fanout = max(3, fanout)   # need at least 3 to test splits
        # Entry sizes
        self.leaf_entry_size = self.kw + 4 + 4    # key + data_pid + slot
        self.internal_entry_size = self.kw + 4    # key + child_pid (per key)
        # Counter exposed to FIM.
        self.nodes_visited = 0

    # ==================================================================
    # Lifecycle
    # ==================================================================
    def build(self) -> None:
        """Create the .bidx file with an empty tree (no root yet)."""
        if not self.buffer.file_exists(self.file_id).value:
            self.buffer.create_file(self.file_id)
        # logical page 0 = tree header
        self.buffer.allocate_page(self.file_id)
        buf = bytearray(self.page_size)
        struct.pack_into(_TREE_HDR_FMT, buf, 0, -1, 0)
        self.nodes_visited += 1
        self.buffer.write_page(self.file_id, 0, bytes(buf))

    def rebuild_from_data(self, iter_records_with_loc) -> None:
        """Rebuild the tree from records on disk."""
        self.build()
        for key, dp, sl in iter_records_with_loc:
            self.insert(key, dp, sl)

    def reset_counter(self) -> None:
        self.nodes_visited = 0

    # ==================================================================
    # Tree-header helpers (page 0)
    # ==================================================================
    def _get_root(self) -> Tuple[int, int]:
        """Return (root_page_id, height). root_page_id == -1 iff empty."""
        bres = self.buffer.get_page(self.file_id, 0)
        self.nodes_visited += 1
        try:
            root, h = struct.unpack_from(_TREE_HDR_FMT, bres.page.data, 0)
            return root, h
        finally:
            self.buffer.unpin(self.file_id, 0, dirty=False)

    def _set_root(self, root_pid: int, height: int) -> None:
        bres = self.buffer.get_page(self.file_id, 0)
        self.nodes_visited += 1
        try:
            buf = bytearray(bres.page.data)
            struct.pack_into(_TREE_HDR_FMT, buf, 0, root_pid, height)
            self.nodes_visited += 1
            self.buffer.write_page(self.file_id, 0, bytes(buf))
        finally:
            self.buffer.unpin(self.file_id, 0, dirty=True)

    # ==================================================================
    # Node I/O
    # ==================================================================
    def _alloc_node(self, kind: int, aux: int = -1) -> int:
        """Allocate a new node page. For leaves we pre-initialise with an
        empty body; for internals we leave it to the caller, because the
        invariant "n keys, n+1 children" makes a truly empty internal node
        invalid."""
        alloc = self.buffer.allocate_page(self.file_id)
        if kind == _KIND_LEAF:
            self._write_node(alloc.page_id, _KIND_LEAF, [], [], aux=aux)
        # internal nodes are written by the caller right after allocation
        return alloc.page_id

    def _read_node(self, page_id: int):
        """Return (kind, count, aux, body_bytes) for the node at page_id.

        body_bytes is just the slice after the 16-byte header -- callers
        decode it according to kind.
        """
        bres = self.buffer.get_page(self.file_id, page_id)
        self.nodes_visited += 1
        try:
            page = bres.page.data
            kind, pid, count, aux = struct.unpack_from(_NODE_HDR_FMT, page, 0)
            body = page[_NODE_HDR_SIZE:]
            return kind, count, aux, body
        finally:
            self.buffer.unpin(self.file_id, page_id, dirty=False)

    def _write_node(self, page_id: int, kind: int, keys: List,
                    payloads: List, aux: int = -1) -> None:
        """Write a node in canonical form.

        For leaves: keys[i] pairs with payloads[i] = (data_pid, slot_id).
        For internals: keys are the n separators; payloads are the n+1
        children, so payloads has length len(keys) + 1.
        """
        self.nodes_visited += 1
        buf = bytearray(self.page_size)
        if kind == _KIND_LEAF:
            count = len(keys)
            struct.pack_into(_NODE_HDR_FMT, buf, 0,
                             _KIND_LEAF, page_id, count, aux)
            off = _NODE_HDR_SIZE
            for k, (dp, sl) in zip(keys, payloads):
                buf[off:off + self.kw] = _pack_key(self.schema, k)
                off += self.kw
                struct.pack_into("<ii", buf, off, dp, sl)
                off += 8
        else:  # internal
            count = len(keys)
            assert len(payloads) == count + 1, \
                f"internal: payloads {len(payloads)} != keys+1 {count+1}"
            struct.pack_into(_NODE_HDR_FMT, buf, 0,
                             _KIND_INTERNAL, page_id, count, aux)
            off = _NODE_HDR_SIZE
            # Layout: c0, k0, c1, k1, ..., k(n-1), cn
            struct.pack_into("<i", buf, off, payloads[0])
            off += 4
            for i in range(count):
                buf[off:off + self.kw] = _pack_key(self.schema, keys[i])
                off += self.kw
                struct.pack_into("<i", buf, off, payloads[i + 1])
                off += 4
        self.buffer.write_page(self.file_id, page_id, bytes(buf))

    # ----- decoding helpers (work on body bytes) ----------------------
    def _decode_leaf(self, count: int, body: bytes):
        keys, payloads = [], []
        off = 0
        for _ in range(count):
            keys.append(_unpack_key(self.schema, body[off:off + self.kw]))
            off += self.kw
            dp, sl = struct.unpack_from("<ii", body, off)
            payloads.append((dp, sl))
            off += 8
        return keys, payloads

    def _decode_internal(self, count: int, body: bytes):
        keys, children = [], []
        off = 0
        (c0,) = struct.unpack_from("<i", body, off)
        children.append(c0)
        off += 4
        for _ in range(count):
            keys.append(_unpack_key(self.schema, body[off:off + self.kw]))
            off += self.kw
            (cn,) = struct.unpack_from("<i", body, off)
            children.append(cn)
            off += 4
        return keys, children

    # ==================================================================
    # Search helpers
    # ==================================================================
    @staticmethod
    def _find_internal_child(keys: List, target) -> int:
        """In an internal node with separators `keys`, return the child index
        i such that the subtree rooted at children[i] could contain `target`.
        Convention: child i covers keys < keys[i], child n covers keys >= keys[n-1].
        Equality goes RIGHT (so duplicates -- which we forbid -- would land
        consistently)."""
        # linear search; nodes are small (fanout <= 10 in our config)
        for i, k in enumerate(keys):
            if target < k:
                return i
        return len(keys)

    @staticmethod
    def _find_leaf_slot(keys: List, target) -> Tuple[int, bool]:
        """In a leaf, return (i, found_exact). i is where `target` belongs
        (insertion point if not found)."""
        for i, k in enumerate(keys):
            if target == k:
                return i, True
            if target < k:
                return i, False
        return len(keys), False

    def _find_leaf_for(self, key) -> Tuple[int, List[int]]:
        """Walk from root to leaf for `key`. Returns (leaf_pid, path) where
        path is the list of internal pids walked through (root first).
        Caller can use the path for split propagation on insert."""
        root, _h = self._get_root()
        path = []
        cur = root
        while True:
            kind, count, _aux, body = self._read_node(cur)
            if kind == _KIND_LEAF:
                return cur, path
            path.append(cur)
            keys, children = self._decode_internal(count, body)
            i = self._find_internal_child(keys, key)
            cur = children[i]

    # ==================================================================
    # Public API
    # ==================================================================
    def lookup(self, key) -> Optional[Tuple[int, int]]:
        root, _ = self._get_root()
        if root == -1:
            return None
        leaf_pid, _ = self._find_leaf_for(key)
        _, count, _aux, body = self._read_node(leaf_pid)
        keys, payloads = self._decode_leaf(count, body)
        i, found = self._find_leaf_slot(keys, key)
        return payloads[i] if found else None

    def range_search(self, lo, hi) -> List[Tuple[int, int]]:
        """Return all (data_pid, slot_id) whose key is in [lo, hi]."""
        out: List[Tuple[int, int]] = []
        root, _ = self._get_root()
        if root == -1:
            return out
        # Walk down for `lo`.
        leaf_pid, _ = self._find_leaf_for(lo)
        cur = leaf_pid
        while cur != -1:
            _, count, nxt, body = self._read_node(cur)
            keys, payloads = self._decode_leaf(count, body)
            stop = False
            for k, p in zip(keys, payloads):
                if k < lo:
                    continue
                if k > hi:
                    stop = True
                    break
                out.append(p)
            if stop:
                break
            cur = nxt
        return out

    def insert(self, key, data_page_id: int, slot_id: int) -> None:
        root, height = self._get_root()
        if root == -1:
            # Empty tree -> create first leaf and make it the root.
            leaf_pid = self._alloc_node(_KIND_LEAF, aux=-1)
            self._write_node(leaf_pid, _KIND_LEAF, [key],
                             [(data_page_id, slot_id)], aux=-1)
            self._set_root(leaf_pid, 1)
            return

        leaf_pid, path = self._find_leaf_for(key)
        _, count, nxt, body = self._read_node(leaf_pid)
        keys, payloads = self._decode_leaf(count, body)
        i, found = self._find_leaf_slot(keys, key)
        # Caller should have already detected duplicates, but be defensive.
        if found:
            return  # silently no-op on duplicate
        keys.insert(i, key)
        payloads.insert(i, (data_page_id, slot_id))

        if len(keys) <= self.fanout:
            self._write_node(leaf_pid, _KIND_LEAF, keys, payloads, aux=nxt)
            return

        # ---- leaf overflow -> split --------------------------------
        mid = len(keys) // 2
        left_keys, right_keys = keys[:mid], keys[mid:]
        left_pl, right_pl = payloads[:mid], payloads[mid:]
        right_pid = self._alloc_node(_KIND_LEAF, aux=nxt)
        self._write_node(right_pid, _KIND_LEAF, right_keys, right_pl, aux=nxt)
        self._write_node(leaf_pid, _KIND_LEAF, left_keys, left_pl,
                         aux=right_pid)
        split_key = right_keys[0]   # first key of right side becomes separator
        self._propagate_split(path, leaf_pid, split_key, right_pid)

    def _propagate_split(self, path: List[int], left_child: int,
                         sep_key, right_child: int) -> None:
        """Insert (sep_key, right_child) into the parent of left_child. If
        the parent is full, recurse. If we pop the path empty, grow the
        tree by one level (new root)."""
        while path:
            parent_pid = path.pop()
            _, count, _aux, body = self._read_node(parent_pid)
            keys, children = self._decode_internal(count, body)
            # find the index of left_child in children
            idx = children.index(left_child)
            keys.insert(idx, sep_key)
            children.insert(idx + 1, right_child)

            if len(keys) <= self.fanout:
                self._write_node(parent_pid, _KIND_INTERNAL, keys, children)
                return

            # ---- internal overflow -> split ------------------------
            mid = len(keys) // 2
            up_key = keys[mid]
            left_keys = keys[:mid]
            right_keys = keys[mid + 1:]    # mid key moves UP
            left_children = children[:mid + 1]
            right_children = children[mid + 1:]
            new_pid = self._alloc_node(_KIND_INTERNAL)
            self._write_node(new_pid, _KIND_INTERNAL,
                             right_keys, right_children)
            self._write_node(parent_pid, _KIND_INTERNAL,
                             left_keys, left_children)
            left_child = parent_pid
            sep_key = up_key
            right_child = new_pid

        # path is empty -> we split the root; grow a level.
        new_root_pid = self._alloc_node(_KIND_INTERNAL)
        self._write_node(new_root_pid, _KIND_INTERNAL,
                         [sep_key], [left_child, right_child])
        _, h = self._get_root()
        self._set_root(new_root_pid, h + 1)

    def delete(self, key) -> bool:
        """Remove `key` from the tree. On underflow we just leave a shorter
        leaf (no merge/redistribute). This is the simple variant the spec
        allows; correctness is preserved, only space efficiency suffers."""
        root, height = self._get_root()
        if root == -1:
            return False
        leaf_pid, _path = self._find_leaf_for(key)
        _, count, nxt, body = self._read_node(leaf_pid)
        keys, payloads = self._decode_leaf(count, body)
        i, found = self._find_leaf_slot(keys, key)
        if not found:
            return False
        del keys[i]
        del payloads[i]
        self._write_node(leaf_pid, _KIND_LEAF, keys, payloads, aux=nxt)
        # If we just emptied the root leaf, reset the tree to empty.
        if not keys and leaf_pid == root:
            self._set_root(-1, 0)
        return True
