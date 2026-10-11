"""Automated test suite for Zero-Copy Mmap Ring Buffer & Trade Event Queue (Member 3)."""

import os
import tempfile
import pytest

from src.data_processing.processor import DataProcessor
from src.ipc.mmap_buffer import (
    MmapRingBuffer,
    MmapCorruptError,
    MmapInaccessibleError,
    MmapCapacityError,
    MAGIC_BYTES,
    HEADER_SIZE,
)
from src.ipc.trade_queue import (
    TradeEventQueue,
    TradeEvent,
    BackpressurePolicy,
    QueueFullError,
)


@pytest.fixture
def temp_mmap_path():
    """Create a temporary path for mmap tests and ensure cleanup."""
    fd, path = tempfile.mkstemp(suffix=".mmap")
    os.close(fd)
    if os.path.exists(path):
        os.remove(path)
    yield path
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def test_mmap_initialization_and_header(temp_mmap_path):
    """Verify proper initialization of backing file and 64-byte header."""
    buf = MmapRingBuffer(temp_mmap_path, capacity=100, record_size=33, create=True)
    header = buf.read_header()

    assert header.magic == MAGIC_BYTES
    assert header.version == 1
    assert header.capacity == 100
    assert header.record_size == 33
    assert header.head_seq == 0
    assert header.tail_seq == 0
    assert header.dropped_count == 0

    stats = buf.get_stats()
    assert stats["capacity"] == 100
    assert stats["unread_records"] == 0
    assert stats["is_closed"] is False
    buf.close()


def test_mmap_invalid_arguments(temp_mmap_path):
    """Test invalid capacity and record sizes raise MmapCapacityError."""
    with pytest.raises(MmapCapacityError):
        MmapRingBuffer(temp_mmap_path, capacity=0, record_size=33, create=True)

    with pytest.raises(MmapCapacityError):
        MmapRingBuffer(temp_mmap_path, capacity=100, record_size=0, create=True)


def test_mmap_write_and_read_roundtrip_with_processor(temp_mmap_path):
    """Verify Member 1's binary order records round-trip through MmapRingBuffer."""
    processor = DataProcessor()
    order1 = processor.create_order(order_id=101, side="BUY", price=150.25, quantity=50)
    order2 = processor.create_order(order_id=102, side="SELL", price=150.50, quantity=30)

    bytes1 = processor.pack_order(order1)
    bytes2 = processor.pack_order(order2)
    assert len(bytes1) == 33
    assert len(bytes2) == 33

    buf = MmapRingBuffer(temp_mmap_path, capacity=10, record_size=33, create=True)
    assert buf.write(bytes1) is True
    assert buf.write(bytes2) is True

    # Read back sequentially
    rec1, next_seq1 = buf.read()
    assert rec1 is not None
    assert next_seq1 == 1
    unpacked1 = processor.unpack_order(rec1)
    assert unpacked1.order_id == 101
    assert unpacked1.side == "BUY"
    assert unpacked1.price == 150.25
    assert unpacked1.quantity == 50

    rec2, next_seq2 = buf.read()
    assert rec2 is not None
    assert next_seq2 == 2
    unpacked2 = processor.unpack_order(rec2)
    assert unpacked2.order_id == 102
    assert unpacked2.side == "SELL"
    assert unpacked2.price == 150.50
    assert unpacked2.quantity == 30

    # No more records
    rec3, next_seq3 = buf.read()
    assert rec3 is None
    assert next_seq3 == 2

    buf.close()


def test_mmap_batch_read(temp_mmap_path):
    """Verify batch reading of multiple records."""
    buf = MmapRingBuffer(temp_mmap_path, capacity=20, record_size=4, create=True)
    for i in range(15):
        buf.write(i.to_bytes(4, "little"))

    batch1, next_seq = buf.read_batch(max_records=10)
    assert len(batch1) == 10
    assert [int.from_bytes(b, "little") for b in batch1] == list(range(10))
    assert next_seq == 10

    batch2, next_seq2 = buf.read_batch(max_records=10)
    assert len(batch2) == 5
    assert [int.from_bytes(b, "little") for b in batch2] == list(range(10, 15))
    assert next_seq2 == 15

    buf.close()


def test_mmap_wrap_around_and_overwriting(temp_mmap_path):
    """Verify ring buffer wrap-around when writing past capacity."""
    capacity = 5
    buf = MmapRingBuffer(temp_mmap_path, capacity=capacity, record_size=4, create=True)

    # Fill to capacity
    for i in range(5):
        assert buf.write(i.to_bytes(4, "little"), overwrite_if_full=False) is True

    # 6th write without overwrite should be rejected
    assert buf.write(b"\x99\x99\x99\x99", overwrite_if_full=False) is False

    # 6th write with overwrite should succeed and increment drops
    assert buf.write((100).to_bytes(4, "little"), overwrite_if_full=True) is True

    stats = buf.get_stats()
    assert stats["head_seq"] == 6
    assert stats["dropped_count"] == 1

    # Reading from beginning should jump over overwritten slot 0
    rec, next_seq = buf.read(consumer_seq=0)
    assert rec is not None
    # Slot 0 was overwritten by 100, so oldest available is 1
    val = int.from_bytes(rec, "little")
    assert val == 1
    buf.close()


def test_mmap_consumer_recovery(temp_mmap_path):
    """Verify a consumer can crash, restart, and resume reading from its last acknowledged sequence."""
    writer = MmapRingBuffer(temp_mmap_path, capacity=100, record_size=4, create=True)
    for i in range(10):
        writer.write(i.to_bytes(4, "little"))

    # Consumer 1 reads first 4 items, then terminates
    consumer1 = MmapRingBuffer(temp_mmap_path, create=False)
    batch, last_seq = consumer1.read_batch(max_records=4)
    assert len(batch) == 4
    assert last_seq == 4
    consumer1.close()

    # Writer adds more data
    for i in range(10, 15):
        writer.write(i.to_bytes(4, "little"))

    # Consumer restarts and resumes from sequence 4
    consumer2 = MmapRingBuffer(temp_mmap_path, create=False)
    remaining_batch, final_seq = consumer2.read_batch(max_records=20, consumer_seq=last_seq)
    assert len(remaining_batch) == 11
    vals = [int.from_bytes(b, "little") for b in remaining_batch]
    assert vals == list(range(4, 15))
    assert final_seq == 15

    consumer2.close()
    writer.close()


def test_mmap_malformed_record_size(temp_mmap_path):
    """Writing data of incorrect size raises ValueError."""
    buf = MmapRingBuffer(temp_mmap_path, capacity=10, record_size=33, create=True)
    with pytest.raises(ValueError, match="does not match buffer record size"):
        buf.write(b"too_short")
    buf.close()


def test_mmap_corrupt_header_detection(temp_mmap_path):
    """Detect corrupted magic bytes or truncated files upon attach."""
    # Write corrupt file
    with open(temp_mmap_path, "wb") as f:
        f.write(b"BADMAGIC" + b"\x00" * 60)

    with pytest.raises(MmapCorruptError, match="Invalid magic bytes"):
        MmapRingBuffer(temp_mmap_path, create=False)

    # Test file smaller than header
    with open(temp_mmap_path, "wb") as f:
        f.write(b"SHORT")

    with pytest.raises(MmapCorruptError, match="smaller than the required header size"):
        MmapRingBuffer(temp_mmap_path, create=False)


def test_mmap_inaccessible_file():
    """Attempting to attach to non-existent file raises MmapInaccessibleError."""
    with pytest.raises(MmapInaccessibleError, match="does not exist"):
        MmapRingBuffer("non_existent_path_12345.mmap", create=False)


# ---------------------------------------------------------------------------
# TradeEventQueue Tests
# ---------------------------------------------------------------------------

def test_trade_event_queue_basic():
    """Verify TradeEventQueue pushing, popping, and nanosecond latency tracking."""
    queue = TradeEventQueue(capacity=100)
    assert queue.is_empty() is True

    trade_dict = {
        "buy_id": 10,
        "sell_id": 20,
        "price": 100.5,
        "qty": 50,
        "timestamp_ns": 123456789,
    }

    assert queue.push(trade_dict) is True
    assert queue.size() == 1
    assert queue.is_empty() is False

    ev = queue.pop_nowait()
    assert ev is not None
    assert ev.buy_order_id == 10
    assert ev.sell_order_id == 20
    assert ev.price == 100.5
    assert ev.quantity == 50
    assert ev.notional == 100.5 * 50
    assert queue.is_empty() is True


def test_trade_event_queue_backpressure_policies():
    """Test DROP_OLDEST, DROP_NEWEST, and RAISE backpressure policies."""
    # 1. DROP_OLDEST
    q_drop_oldest = TradeEventQueue(capacity=3, backpressure=BackpressurePolicy.DROP_OLDEST)
    for i in range(5):
        q_drop_oldest.push({"buy_id": i, "sell_id": i, "price": 10.0, "qty": 1})
    assert q_drop_oldest.size() == 3
    assert q_drop_oldest.stats()["dropped_count"] == 2
    batch = q_drop_oldest.pop_batch(max_items=10)
    assert [ev.buy_order_id for ev in batch] == [2, 3, 4]

    # 2. DROP_NEWEST
    q_drop_newest = TradeEventQueue(capacity=2, backpressure=BackpressurePolicy.DROP_NEWEST)
    assert q_drop_newest.push({"buy_id": 1, "sell_id": 1, "price": 10.0, "qty": 1}) is True
    assert q_drop_newest.push({"buy_id": 2, "sell_id": 2, "price": 10.0, "qty": 1}) is True
    assert q_drop_newest.push({"buy_id": 3, "sell_id": 3, "price": 10.0, "qty": 1}) is False
    assert q_drop_newest.stats()["dropped_count"] == 1
    batch2 = q_drop_newest.pop_batch(max_items=10)
    assert [ev.buy_order_id for ev in batch2] == [1, 2]

    # 3. RAISE
    q_raise = TradeEventQueue(capacity=1, backpressure=BackpressurePolicy.RAISE)
    assert q_raise.push({"buy_id": 1, "sell_id": 1, "price": 10.0, "qty": 1}) is True
    with pytest.raises(QueueFullError):
        q_raise.push({"buy_id": 2, "sell_id": 2, "price": 10.0, "qty": 1})


def test_trade_event_queue_subscribers():
    """Verify live subscriber callbacks receive pushed trade events."""
    queue = TradeEventQueue(capacity=10)
    received = []

    def subscriber(ev: TradeEvent):
        received.append(ev.buy_order_id)

    queue.subscribe(subscriber)
    queue.push({"buy_id": 42, "sell_id": 43, "price": 99.0, "qty": 10})
    queue.push({"buy_id": 44, "sell_id": 45, "price": 99.0, "qty": 10})

    assert received == [42, 44]

    queue.unsubscribe(subscriber)
    queue.push({"buy_id": 46, "sell_id": 47, "price": 99.0, "qty": 10})
    assert received == [42, 44]
