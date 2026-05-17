"""Step 4b -- Slotted page operations.

Stateless helpers that interpret a fixed-size byte page (default 4096 B) as
a "slotted page" of records following one Schema.

Page layout (see DESIGN.md):

    +-----------------------------------------------------------+ offset 0
    |  page_id (4 B)  | record_count (4 B) | slot_bitmap (2 B) |
    |  padding (6 B)                                            |
    +-----------------------------------------------------------+ offset 16
    |  slot 0  (record_size bytes)                              |
    +-----------------------------------------------------------+
    |  slot 1  (record_size bytes)                              |
    +-----------------------------------------------------------+
    |   ...  up to max_records_per_page slots ...               |
    +-----------------------------------------------------------+
    |  unused tail bytes (zero-filled)                          |
    +-----------------------------------------------------------+ page_size

The slot bitmap has one bit per slot, bit 0 = slot 0. A 1 means occupied.
We use a uint16 (2 bytes) which is enough for up to 16 slots; the spec
caps max_records_per_page at 10.

This module is pure: every function takes a bytes/bytearray page in and
returns a new bytes page out (or just an integer/list). No I/O, no Buffer
Manager calls. The FileIndexManager glues it to those.
"""

import struct
from typing import List, Optional, Tuple

from .schema import Schema, pack_record, unpack_record

PAGE_HEADER_FMT = "<iiH6x"            # page_id, rec_count, bitmap, 6 pad
PAGE_HEADER_SIZE = struct.calcsize(PAGE_HEADER_FMT)   # 16 bytes
assert PAGE_HEADER_SIZE == 16, "page header must be exactly 16 bytes"


# -------------------------------------------------------------------------
# Header decoding/encoding
# -------------------------------------------------------------------------
def read_header(page: bytes) -> Tuple[int, int, int]:
    """Return (page_id, record_count, slot_bitmap)."""
    page_id, rec_count, bitmap = struct.unpack_from(PAGE_HEADER_FMT, page, 0)
    return page_id, rec_count, bitmap


def write_header(buf: bytearray, page_id: int, rec_count: int,
                 bitmap: int) -> None:
    """In-place header rewrite."""
    struct.pack_into(PAGE_HEADER_FMT, buf, 0, page_id, rec_count, bitmap)


def empty_page(page_id: int, page_size: int) -> bytes:
    """Build a zero-filled page with a valid empty header."""
    buf = bytearray(page_size)
    write_header(buf, page_id, 0, 0)
    return bytes(buf)


# -------------------------------------------------------------------------
# Bitmap helpers
# -------------------------------------------------------------------------
def bit_set(bitmap: int, i: int) -> bool:
    return (bitmap >> i) & 1 == 1


def set_bit(bitmap: int, i: int) -> int:
    return bitmap | (1 << i)


def clear_bit(bitmap: int, i: int) -> int:
    return bitmap & ~(1 << i)


def occupied_slots(bitmap: int, max_slots: int) -> List[int]:
    return [i for i in range(max_slots) if bit_set(bitmap, i)]


def first_free_slot(bitmap: int, max_slots: int) -> Optional[int]:
    for i in range(max_slots):
        if not bit_set(bitmap, i):
            return i
    return None


# -------------------------------------------------------------------------
# Slot read / write / delete
# -------------------------------------------------------------------------
def slot_offset(slot: int, record_size: int) -> int:
    return PAGE_HEADER_SIZE + slot * record_size


def read_slot(page: bytes, slot: int, schema: Schema) -> List:
    """Read slot `slot` as a Python record. Caller checks the bitmap first."""
    off = slot_offset(slot, schema.record_size)
    raw = page[off:off + schema.record_size]
    return unpack_record(schema, raw)


def write_slot(page: bytes, slot: int, schema: Schema, values: List) -> bytes:
    """Return a new page with `values` written into `slot` and the bitmap
    bit set. Updates record_count if the slot was previously free."""
    buf = bytearray(page)
    record_blob = pack_record(schema, values)
    off = slot_offset(slot, schema.record_size)
    buf[off:off + schema.record_size] = record_blob
    page_id, rec_count, bitmap = read_header(buf)
    if not bit_set(bitmap, slot):
        bitmap = set_bit(bitmap, slot)
        rec_count += 1
    write_header(buf, page_id, rec_count, bitmap)
    return bytes(buf)


def delete_slot(page: bytes, slot: int, record_size: int) -> bytes:
    """Return a new page with `slot` marked free. Zeros the slot bytes too
    so we don't leak stale data on later reads."""
    buf = bytearray(page)
    page_id, rec_count, bitmap = read_header(buf)
    if not bit_set(bitmap, slot):
        return bytes(buf)   # already empty -- idempotent
    off = slot_offset(slot, record_size)
    buf[off:off + record_size] = b"\x00" * record_size
    bitmap = clear_bit(bitmap, slot)
    rec_count -= 1
    write_header(buf, page_id, rec_count, bitmap)
    return bytes(buf)


# -------------------------------------------------------------------------
# Iteration helper
# -------------------------------------------------------------------------
def iter_records(page: bytes, schema: Schema, max_slots: int):
    """Yield (slot_index, record) for every occupied slot, in slot order."""
    _, _, bitmap = read_header(page)
    for i in range(max_slots):
        if bit_set(bitmap, i):
            yield i, read_slot(page, i, schema)


# -------------------------------------------------------------------------
# Capacity check
# -------------------------------------------------------------------------
def capacity_check(page_size: int, max_records: int, record_size: int) -> None:
    """Raise if the configured record-size + max-records won't fit in a page.
    The FileIndexManager calls this on `create type`."""
    usable = page_size - PAGE_HEADER_SIZE
    needed = max_records * record_size
    if needed > usable:
        raise ValueError(
            f"records won't fit: {max_records} slots * {record_size} B = "
            f"{needed} B exceeds usable page space {usable} B"
        )
