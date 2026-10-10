"""File-backed mmap ring buffer for fixed-size ChronosMatch order records."""

import mmap
import os
import struct
import sys
from pathlib import Path

MAGIC = b"CHRONOS1"
HEADER = struct.Struct("<8sIIII")
HEADER_SIZE = HEADER.size


class MmapRingBuffer:
    """Bounded, file-backed ring buffer for fixed-size binary records.

    Use one producer and one consumer. Operations are protected by an
    OS-level file lock so separate processes can coordinate access.
    """

    def __init__(self, path, capacity=1024, *, create=True):
        if capacity <= 0:
            raise ValueError("capacity must be positive")

        self.path = Path(path)
        self.capacity = capacity
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if create:
            self._file = open(self.path, "a+b")
            self._file.seek(0, os.SEEK_END)
            if self._file.tell() == 0:
                self._file.truncate(
                    HEADER_SIZE + capacity * 33
                )
                self._file.flush()
            elif self._file.tell() < HEADER_SIZE:
                self._file.close()
                raise ValueError("Invalid ring buffer file")
        else:
            self._file = open(self.path, "r+b")

        size = os.fstat(self._file.fileno()).st_size
        if size < HEADER_SIZE:
            self._file.close()
            raise ValueError("Invalid ring buffer header")

        self._map = mmap.mmap(self._file.fileno(), 0)

        with self._locked():
            if self._map[:8] != MAGIC:
                if not create:
                    self.close()
                    raise ValueError("Ring buffer is not initialized")
                self._map[:] = b"\0" * len(self._map)
                self._map[:HEADER_SIZE] = HEADER.pack(
                    MAGIC, capacity, 0, 0, 0
                )
                self._map.flush()

            magic, stored_capacity, read_idx, write_idx, count = (
                HEADER.unpack(self._map[:HEADER_SIZE])
            )
            expected_size = HEADER_SIZE + stored_capacity * 33

            if (
                magic != MAGIC
                or stored_capacity <= 0
                or len(self._map) != expected_size
                or read_idx >= stored_capacity
                or write_idx >= stored_capacity
                or count > stored_capacity
            ):
                self.close()
                raise ValueError("Corrupt ring buffer metadata")

            if stored_capacity != capacity:
                self.capacity = stored_capacity

    class _Lock:
        def __init__(self, owner):
            self.owner = owner

        def __enter__(self):
            f = self.owner._file
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, exc_type, exc, tb):
            f = self.owner._file
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _locked(self):
        if self._closed:
            raise ValueError("Ring buffer is closed")
        return self._Lock(self)

    def put(self, record):
        """Write one 33-byte record. Raise BufferError if full."""
        record = bytes(record)
        if len(record) != 33:
            raise ValueError("record must be exactly 33 bytes")

        with self._locked():
            _, cap, read_idx, write_idx, count = HEADER.unpack(
                self._map[:HEADER_SIZE]
            )
            if count == cap:
                raise BufferError("Ring buffer is full")

            offset = HEADER_SIZE + write_idx * 33
            self._map[offset:offset + 33] = record
            write_idx = (write_idx + 1) % cap
            count += 1
            self._map[:HEADER_SIZE] = HEADER.pack(
                MAGIC, cap, read_idx, write_idx, count
            )

    def get(self):
        """Read and remove the oldest record, or return None if empty."""
        with self._locked():
            _, cap, read_idx, write_idx, count = HEADER.unpack(
                self._map[:HEADER_SIZE]
            )
            if count == 0:
                return None

            offset = HEADER_SIZE + read_idx * 33
            record = self._map[offset:offset + 33]
            read_idx = (read_idx + 1) % cap
            count -= 1
            self._map[:HEADER_SIZE] = HEADER.pack(
                MAGIC, cap, read_idx, write_idx, count
            )
            return record

    def __len__(self):
        with self._locked():
            return HEADER.unpack(self._map[:HEADER_SIZE])[4]

    def close(self):
        if not getattr(self, "_closed", True):
            self._map.close()
            self._file.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
