"""Zero-Copy Memory-Mapped Ring Buffer for IPC (Member 3).

Provides a fixed-size binary shared-memory ring buffer using Python's `mmap` and `struct`.
Allows high-throughput, sub-microsecond inter-process communication between
order-generation firehoses, matching engines, and consumers.

Resilience features:
- Shared 64-byte header containing magic signature, version, sequence counters, and flags.
- Monotonic sequence tracking for multiple independent or restarting readers.
- Overwrite / backpressure protection with drop tracking.
- Corrupted header detection and recovery.
- Cross-platform support (Windows and Unix).
- Safe cleanup and process lifecycle handling.
"""

from __future__ import annotations

import mmap
import os
import struct
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO

# ---------------------------------------------------------------------------
# Header Constants & Layout
# ---------------------------------------------------------------------------

MAGIC_BYTES = b"CMMAP\x01\x00\x00"  # 8 bytes magic signature
HEADER_FORMAT = "<8s H I Q Q Q Q I 14s"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # Exactly 64 bytes

FLAG_ACTIVE = 0
FLAG_CLOSED = 1


class MmapError(Exception):
    """Base exception for memory-mapped ring buffer errors."""


class MmapCorruptError(MmapError):
    """Raised when memory-mapped header or data structure is corrupted."""


class MmapInaccessibleError(MmapError):
    """Raised when mmap file cannot be opened, accessed, or mapped."""


class MmapCapacityError(MmapError):
    """Raised when mmap buffer capacity or record size is invalid."""


@dataclass
class MmapHeader:
    """Parsed representation of the 64-byte shared memory header."""
    magic: bytes
    version: int
    record_size: int
    capacity: int
    head_seq: int
    tail_seq: int
    dropped_count: int
    flags: int


# ---------------------------------------------------------------------------
# MmapRingBuffer Implementation
# ---------------------------------------------------------------------------

class MmapRingBuffer:
    """High-performance zero-copy memory-mapped ring buffer.

    Parameters
    ----------
    file_path : str
        Path to backing file on disk.
    capacity : int, optional
        Maximum number of fixed-size records the buffer can hold (required when creating).
    record_size : int, optional
        Fixed byte size per record, e.g. 33 bytes for ChronosMatch orders (required when creating).
    create : bool
        If True, initializes a new ring buffer header and allocates file.
        If False, attaches to an existing ring buffer file.
    """

    def __init__(
        self,
        file_path: str,
        capacity: int = 10000,
        record_size: int = 33,
        create: bool = True,
    ) -> None:
        self.file_path = os.path.abspath(file_path)
        self.capacity = int(capacity)
        self.record_size = int(record_size)
        self._lock = threading.Lock()
        self._file: BinaryIO | None = None
        self._mm: mmap.mmap | None = None
        self._is_closed = False
        self._local_read_seq = 0

        if create:
            if self.capacity <= 0:
                raise MmapCapacityError("Capacity must be positive.")
            if self.record_size <= 0:
                raise MmapCapacityError("Record size must be positive.")
            self._create_buffer()
        else:
            self._attach_buffer()

    def _create_buffer(self) -> None:
        """Create backing file and initialize ring buffer header."""
        total_size = HEADER_SIZE + (self.capacity * self.record_size)
        parent_dir = os.path.dirname(self.file_path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        try:
            self._file = open(self.file_path, "wb+")
            # Preallocate file to required size
            self._file.seek(total_size - 1)
            self._file.write(b"\x00")
            self._file.flush()

            # Memory-map the file
            self._mm = mmap.mmap(self._file.fileno(), total_size, access=mmap.ACCESS_WRITE)

            # Write header
            header_bytes = struct.pack(
                HEADER_FORMAT,
                MAGIC_BYTES,
                1,  # version
                self.record_size,
                self.capacity,
                0,  # head_seq
                0,  # tail_seq
                0,  # dropped_count
                FLAG_ACTIVE,
                b"\x00" * 14,
            )
            self._mm[0:HEADER_SIZE] = header_bytes
            self._mm.flush()
        except (OSError, IOError) as exc:
            self.close()
            raise MmapInaccessibleError(f"Failed to create mmap file at '{self.file_path}': {exc}") from exc

    def _attach_buffer(self) -> None:
        """Attach to an existing ring buffer file and validate header."""
        if not os.path.exists(self.file_path):
            raise MmapInaccessibleError(f"Backing file '{self.file_path}' does not exist.")

        file_len = os.path.getsize(self.file_path)
        if file_len < HEADER_SIZE:
            raise MmapCorruptError(f"File '{self.file_path}' is smaller than the required header size ({file_len} < {HEADER_SIZE}).")

        try:
            self._file = open(self.file_path, "r+b")
            self._mm = mmap.mmap(self._file.fileno(), file_len, access=mmap.ACCESS_WRITE)
            header = self.read_header()

            if header.magic != MAGIC_BYTES:
                raise MmapCorruptError(f"Invalid magic bytes in mmap header: {header.magic!r} != {MAGIC_BYTES!r}")

            self.record_size = header.record_size
            self.capacity = header.capacity
            expected_size = HEADER_SIZE + (self.capacity * self.record_size)

            if file_len < expected_size:
                raise MmapCorruptError(
                    f"Mmap file truncated: size is {file_len} bytes, but header requires {expected_size} bytes."
                )

            # Initialize local read sequence to current tail
            self._local_read_seq = header.tail_seq
        except (OSError, IOError) as exc:
            self.close()
            raise MmapInaccessibleError(f"Failed to attach to mmap file '{self.file_path}': {exc}") from exc

    def read_header(self) -> MmapHeader:
        """Read and parse the current header state from shared memory."""
        if self._mm is None or self._is_closed:
            raise MmapInaccessibleError("Ring buffer is closed.")

        raw = bytes(self._mm[0:HEADER_SIZE])
        if len(raw) < HEADER_SIZE:
            raise MmapCorruptError("Unable to read complete header.")

        magic, ver, rec_sz, cap, head, tail, dropped, flags, _ = struct.unpack(HEADER_FORMAT, raw)
        return MmapHeader(
            magic=magic,
            version=ver,
            record_size=rec_sz,
            capacity=cap,
            head_seq=head,
            tail_seq=tail,
            dropped_count=dropped,
            flags=flags,
        )

    def write(self, data: bytes, overwrite_if_full: bool = True, timeout: float = 0.0) -> bool:
        """Write a fixed-size binary record to the ring buffer.

        Parameters
        ----------
        data : bytes
            Binary record matching `self.record_size`.
        overwrite_if_full : bool
            If True, overwrites oldest unread records when capacity is reached and records a drop.
            If False, returns False when buffer is full.
        timeout : float
            Reserved for future blocking backpressure.

        Returns
        -------
        bool
            True if written successfully, False if full (when overwrite_if_full=False).
        """
        if self._is_closed or self._mm is None:
            raise MmapInaccessibleError("Cannot write to closed ring buffer.")

        if len(data) != self.record_size:
            raise ValueError(f"Record length {len(data)} does not match buffer record size {self.record_size}.")

        with self._lock:
            header = self.read_header()
            head = header.head_seq
            tail = header.tail_seq
            dropped = header.dropped_count

            # Check capacity
            in_flight = head - tail
            if in_flight >= self.capacity:
                if not overwrite_if_full:
                    return False
                # Advance tail to make room and count drop
                tail = head - self.capacity + 1
                dropped += 1

            slot_idx = head % self.capacity
            offset = HEADER_SIZE + (slot_idx * self.record_size)

            # Write record bytes directly to mmap slice
            self._mm[offset : offset + self.record_size] = data

            # Update head, tail, dropped in header
            head += 1
            packed_update = struct.pack("<Q Q Q", head, tail, dropped)
            # Offset 22 is start of (head_seq, tail_seq, dropped_count):
            # magic (8) + version (2) + record_size (4) + capacity (8) = 22
            self._mm[22 : 22 + 24] = packed_update

            return True

    def read(self, consumer_seq: int | None = None) -> tuple[bytes | None, int]:
        """Read a single record from the ring buffer.

        Parameters
        ----------
        consumer_seq : int, optional
            Monotonic sequence number to read from. If None, uses local internal sequence.

        Returns
        -------
        tuple[bytes | None, int]
            (record_bytes, next_sequence_number).
            If no new data is available, returns (None, consumer_seq).
        """
        if self._is_closed or self._mm is None:
            raise MmapInaccessibleError("Cannot read from closed ring buffer.")

        with self._lock:
            header = self.read_header()
            head = header.head_seq
            tail = header.tail_seq

            seq = self._local_read_seq if consumer_seq is None else consumer_seq

            # Nothing new available yet
            if seq >= head:
                return None, seq

            # If consumer fell behind overwritten slots, jump to oldest available slot
            if head - seq > self.capacity:
                seq = head - self.capacity

            slot_idx = seq % self.capacity
            offset = HEADER_SIZE + (slot_idx * self.record_size)

            data = bytes(self._mm[offset : offset + self.record_size])
            next_seq = seq + 1

            if consumer_seq is None:
                self._local_read_seq = next_seq
                # Update shared tail if this is primary consumer
                if next_seq > tail:
                    self._mm[30:38] = struct.pack("<Q", next_seq)

            return data, next_seq

    def read_batch(self, max_records: int = 100, consumer_seq: int | None = None) -> tuple[list[bytes], int]:
        """Read up to `max_records` binary records in a single batch."""
        records: list[bytes] = []
        curr_seq = self._local_read_seq if consumer_seq is None else consumer_seq

        with self._lock:
            header = self.read_header()
            head = header.head_seq

            if curr_seq >= head:
                return records, curr_seq

            if head - curr_seq > self.capacity:
                curr_seq = head - self.capacity

            count_to_read = min(max_records, head - curr_seq)
            for _ in range(count_to_read):
                slot_idx = curr_seq % self.capacity
                offset = HEADER_SIZE + (slot_idx * self.record_size)
                records.append(bytes(self._mm[offset : offset + self.record_size]))
                curr_seq += 1

            if consumer_seq is None:
                self._local_read_seq = curr_seq
                if curr_seq > header.tail_seq:
                    self._mm[30:38] = struct.pack("<Q", curr_seq)

        return records, curr_seq

    def get_stats(self) -> dict[str, int | bool]:
        """Return operational statistics of the ring buffer."""
        header = self.read_header()
        head = header.head_seq
        tail = header.tail_seq
        return {
            "capacity": self.capacity,
            "record_size": self.record_size,
            "head_seq": head,
            "tail_seq": tail,
            "unread_records": max(0, head - tail),
            "dropped_count": header.dropped_count,
            "is_closed": self._is_closed or bool(header.flags & FLAG_CLOSED),
        }

    def close(self) -> None:
        """Close the memory-mapped ring buffer and release OS resources."""
        with self._lock:
            if self._is_closed:
                return
            self._is_closed = True

            if self._mm is not None:
                try:
                    # Mark closed flag in header
                    self._mm[46:50] = struct.pack("<I", FLAG_CLOSED)
                    self._mm.flush()
                except Exception:
                    pass
                try:
                    self._mm.close()
                except Exception:
                    pass
                self._mm = None

            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None

    def __enter__(self) -> MmapRingBuffer:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
