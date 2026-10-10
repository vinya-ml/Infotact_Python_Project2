"""
ChronosMatch Zero-Copy IPC Ring Buffer (mmap-based).

This is the actual shared-memory bus described in the project spec:
a fixed-size file, memory-mapped, that two separate Python processes
can open and read/write the exact same bytes from -- no pickling,
no sockets, no serialization overhead beyond the fixed binary format
already defined in processor.py.

Layout:
    [ write_index (8 bytes) ][ read_index (8 bytes) ][ slot 0 ][ slot 1 ] ...

Design note (single-writer / single-reader safety):
    write_index is only ever WRITTEN by the writer process.
    read_index  is only ever WRITTEN by the reader process.
    Each process only READS the other's field, never writes it.
    This avoids the classic lost-update race where both processes
    read-modify-write a shared header and overwrite each other's
    update -- it only works because there is exactly one writer and
    exactly one reader. (Multiple writers or multiple readers would
    need real locks / atomics, which this simple proof does not add.)
"""

import mmap
import os
import struct

from src.data_processing.processor import ORDER_SIZE

WRITE_INDEX_OFFSET = 0
READ_INDEX_OFFSET = 8
INDEX_FORMAT = "<Q"  # single uint64
INDEX_SIZE = struct.calcsize(INDEX_FORMAT)
HEADER_SIZE = WRITE_INDEX_OFFSET + READ_INDEX_OFFSET + INDEX_SIZE  # 16 bytes total

DEFAULT_SLOTS = 100_000  # ring buffer capacity (number of orders it can hold)


class RingBuffer:
    """
    A shared-memory ring buffer for passing fixed-size binary order
    records between exactly one writer process and one reader process.
    """

    def __init__(self, path: str, num_slots: int = DEFAULT_SLOTS, create: bool = False):
        self.path = path
        self.slot_size = ORDER_SIZE

        if create:
            # Creator decides the size; num_slots is used as given.
            self.num_slots = num_slots
            self.buffer_size = HEADER_SIZE + (self.slot_size * self.num_slots)
            self._create_file()
        else:
            # Attacher (reader, or any later writer) derives the size from
            # the file itself, rather than trusting a num_slots argument
            # that could disagree with whatever the creator actually used.
            # This is what prevents two processes (e.g. firehose.py and
            # consumer.py) from silently assuming different buffer sizes
            # for the same file -- a real bug caught during verification:
            # on Windows, mmap silently extends a too-small file to match
            # a larger requested size, which hid the mismatch; on Linux/
            # Mac, mmap correctly refuses and raises an error instead.
            actual_size = os.path.getsize(self.path)
            self.buffer_size = actual_size
            self.num_slots = (actual_size - HEADER_SIZE) // self.slot_size

        self._file = open(self.path, "r+b")
        self._mmap = mmap.mmap(self._file.fileno(), self.buffer_size)

    def _create_file(self):
        """Create (or reset) the backing file with the correct total size."""
        with open(self.path, "wb") as f:
            f.write(b"\x00" * self.buffer_size)

    # -----------------------------------------------------
    # Header access -- each index lives in its own fixed slot,
    # and only its owning process ever writes to it.
    # -----------------------------------------------------

    def _get_write_index(self) -> int:
        self._mmap.seek(WRITE_INDEX_OFFSET)
        return struct.unpack(INDEX_FORMAT, self._mmap.read(INDEX_SIZE))[0]

    def _set_write_index(self, value: int):
        self._mmap.seek(WRITE_INDEX_OFFSET)
        self._mmap.write(struct.pack(INDEX_FORMAT, value))

    def _get_read_index(self) -> int:
        self._mmap.seek(READ_INDEX_OFFSET)
        return struct.unpack(INDEX_FORMAT, self._mmap.read(INDEX_SIZE))[0]

    def _set_read_index(self, value: int):
        self._mmap.seek(READ_INDEX_OFFSET)
        self._mmap.write(struct.pack(INDEX_FORMAT, value))

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

        Must only be called from the WRITER process/side.
        """
        if len(packed_order) != self.slot_size:
            raise ValueError(
                f"Expected {self.slot_size} bytes, got {len(packed_order)} bytes"
            )

        write_index = self._get_write_index()
        offset = self._slot_offset(write_index)

        self._mmap.seek(offset)
        self._mmap.write(packed_order)

        # Only update write_index -- never touch read_index.
        self._set_write_index(write_index + 1)

        return write_index

    def read(self):
        """
        Read the next unread order from the buffer, in bytes form.
        Returns None if there is nothing new to read.

        Must only be called from the READER process/side.
        """
        write_index = self._get_write_index()
        read_index = self._get_read_index()

        if read_index >= write_index:
            return None  # nothing new to read

        offset = self._slot_offset(read_index)
        self._mmap.seek(offset)
        data = self._mmap.read(self.slot_size)

        # Only update read_index -- never touch write_index.
        self._set_read_index(read_index + 1)

        return data

    def pending_count(self) -> int:
        """How many orders are written but not yet read."""
        return self._get_write_index() - self._get_read_index()

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