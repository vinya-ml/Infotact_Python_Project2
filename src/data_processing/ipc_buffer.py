"""
ChronosMatch Zero-Copy IPC Ring Buffer (mmap-based).

This is the actual shared-memory bus described in the project spec:
a fixed-size file, memory-mapped, that two separate Python processes
can open and read/write the exact same bytes from — no pickling,
no sockets, no serialization overhead beyond the fixed binary format
already defined in processor.py.

Layout:
    [ header (16 bytes) ][ slot 0 ][ slot 1 ] ... [ slot N-1 ]

Header (16 bytes, little-endian):
    write_index : uint64  -> next slot index to write to
    read_index  : uint64  -> next slot index to read from

Each slot is exactly ORDER_SIZE bytes (from processor.py's ORDER_FORMAT),
so this buffer makes no assumptions of its own about order fields --
it just stores/retrieves whatever processor.py packs and unpacks.
"""

import mmap
import os
import struct

from src.data_processing.processor import ORDER_SIZE

# Header: two uint64 counters (write_index, read_index)
HEADER_FORMAT = "<QQ"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

DEFAULT_SLOTS = 100_000  # ring buffer capacity (number of orders it can hold)


class RingBuffer:
    """
    A shared-memory ring buffer for passing fixed-size binary order
    records between two separate Python processes.
    """

    def __init__(self, path: str, num_slots: int = DEFAULT_SLOTS, create: bool = False):
        self.path = path
        self.num_slots = num_slots
        self.slot_size = ORDER_SIZE
        self.buffer_size = HEADER_SIZE + (self.slot_size * self.num_slots)

        if create:
            self._create_file()

        self._file = open(self.path, "r+b")
        self._mmap = mmap.mmap(self._file.fileno(), self.buffer_size)

    def _create_file(self):
        """Create (or reset) the backing file with the correct total size."""
        with open(self.path, "wb") as f:
            f.write(b"\x00" * self.buffer_size)
            # Initialize header: write_index = 0, read_index = 0
            f.seek(0)
            f.write(struct.pack(HEADER_FORMAT, 0, 0))

    # -----------------------------------------------------
    # Header access (write_index / read_index)
    # -----------------------------------------------------

    def _get_indices(self):
        self._mmap.seek(0)
        raw = self._mmap.read(HEADER_SIZE)
        write_index, read_index = struct.unpack(HEADER_FORMAT, raw)
        return write_index, read_index

    def _set_indices(self, write_index: int, read_index: int):
        self._mmap.seek(0)
        self._mmap.write(struct.pack(HEADER_FORMAT, write_index, read_index))

    # -----------------------------------------------------
    # Core operations
    # -----------------------------------------------------

    def _slot_offset(self, slot_index: int) -> int:
        slot = slot_index % self.num_slots
        return HEADER_SIZE + (slot * self.slot_size)

    def write(self, packed_order: bytes) -> int:
        """
        Write one packed order (already serialized by processor.pack_order)
        into the next available slot. Returns the slot index written to.
        """
        if len(packed_order) != self.slot_size:
            raise ValueError(
                f"Expected {self.slot_size} bytes, got {len(packed_order)} bytes"
            )

        write_index, read_index = self._get_indices()
        offset = self._slot_offset(write_index)

        self._mmap.seek(offset)
        self._mmap.write(packed_order)

        new_write_index = write_index + 1
        self._set_indices(new_write_index, read_index)

        return write_index

    def read(self):
        """
        Read the next unread order from the buffer, in bytes form.
        Returns None if there is nothing new to read.
        """
        write_index, read_index = self._get_indices()

        if read_index >= write_index:
            return None  # nothing new to read

        offset = self._slot_offset(read_index)
        self._mmap.seek(offset)
        data = self._mmap.read(self.slot_size)

        self._set_indices(write_index, read_index + 1)

        return data

    def pending_count(self) -> int:
        """How many orders are written but not yet read."""
        write_index, read_index = self._get_indices()
        return write_index - read_index

    def close(self):
        self._mmap.close()
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def open_buffer(path: str, num_slots: int = DEFAULT_SLOTS, create: bool = False) -> RingBuffer:
    """Convenience function to open (or create) a ring buffer at `path`."""
    return RingBuffer(path=path, num_slots=num_slots, create=create)