"""End-to-End Integration Test Suite for ChronosMatch (Member 3).

Validates the full pipeline:
Market Simulator -> DataProcessor (pack) -> Mmap Ring Buffer (Zero-Copy IPC)
-> Matching Engine (Price-Time Priority) -> TradeEventQueue
-> Background Persistence Worker -> SQLite Database Ledger -> Monitoring UI.

Covers all required Week 3 & Week 4 failure and resilience scenarios:
- Normal trade matching and delivery.
- Empty order book and no-match conditions.
- Multiple matched trades arriving rapidly.
- Malformed or incomplete messages.
- Unexpected engine or UI process termination.
- Missing, inaccessible, or incorrectly formatted mmap data.
- Restarting a consumer after a temporary failure.
- Shutdown while messages or trades remain pending.
"""

import os
import tempfile
import time
import pytest

from src.data_processing.processor import DataProcessor
from src.ipc.mmap_buffer import MmapRingBuffer, MmapCorruptError, MmapInaccessibleError
from src.engine.matching_engine import MatchingEngine, BUY, SELL
from src.ipc.trade_queue import TradeEventQueue
from src.persistence.repository import SQLiteTradeRepository
from src.persistence.worker import BackgroundPersistenceWorker
from src.monitor.latency_monitor import LatencyMonitor


@pytest.fixture
def temp_env():
    """Set up isolated temporary paths for mmap buffer and sqlite database."""
    fd1, mmap_path = tempfile.mkstemp(suffix=".mmap")
    os.close(fd1)
    os.remove(mmap_path)

    fd2, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd2)
    os.remove(db_path)

    yield {"mmap": mmap_path, "db": db_path}

    for path in (mmap_path, db_path):
        for ext in ("", "-wal", "-shm"):
            p = path + ext
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def test_e2e_normal_trade_matching_and_delivery(temp_env):
    """Scenario 1: Normal trade matching, IPC delivery, persistence, and UI snapshot."""
    mmap_path = temp_env["mmap"]
    db_path = temp_env["db"]

    # 1. Market Producer writes binary orders into mmap buffer
    processor = DataProcessor()
    mmap_buf = MmapRingBuffer(mmap_path, capacity=100, record_size=33, create=True)

    order1 = processor.create_order(order_id=1, side="BUY", price=100.0, quantity=50)
    order2 = processor.create_order(order_id=2, side="SELL", price=100.0, quantity=50)
    mmap_buf.write(processor.pack_order(order1))
    mmap_buf.write(processor.pack_order(order2))

    # 2. Engine Ingestion consumes from mmap buffer and matches
    engine = MatchingEngine()
    rec1, seq = mmap_buf.read()
    rec2, seq = mmap_buf.read(consumer_seq=seq)
    o1 = processor.unpack_order(rec1)
    o2 = processor.unpack_order(rec2)

    engine.add_order(o1.order_id, BUY, o1.price, o1.quantity, o1.timestamp_ns)
    engine.add_order(o2.order_id, SELL, o2.price, o2.quantity, o2.timestamp_ns)

    trades = engine.get_trades()
    assert len(trades) == 1
    assert trades[0]["qty"] == 50

    # 3. Persistence Worker writes trades to SQLite
    trade_queue = TradeEventQueue(capacity=100)
    repo = SQLiteTradeRepository(db_path=db_path)
    worker = BackgroundPersistenceWorker(trade_queue=trade_queue, repository=repo, batch_size=10, flush_interval_seconds=0.01)
    worker.start()

    trade_queue.push(trades[0])
    time.sleep(0.08)
    worker.stop(timeout=1.0)

    assert repo.get_trade_count() == 1
    saved = repo.get_trades(limit=1)[0]
    assert saved.buy_order_id == 1
    assert saved.sell_order_id == 2
    assert saved.price == 100.0
    assert saved.quantity == 50

    mmap_buf.close()
    repo.close()


def test_empty_order_book_and_no_match(temp_env):
    """Scenario 2: Empty order book, resting orders, and no-match conditions."""
    db_path = temp_env["db"]
    monitor = LatencyMonitor(enable_persistence=True, db_path=db_path)

    # Initial empty state
    book = monitor.get_top_of_book()
    assert book.best_bid is None
    assert book.best_ask is None
    assert book.spread is None

    # Submit resting orders that do NOT cross: Bid 95.0, Ask 105.0
    monitor.add_custom_order(order_id=1, side="BUY", price=95.0, quantity=10)
    monitor.add_custom_order(order_id=2, side="SELL", price=105.0, quantity=10)

    book_after = monitor.get_top_of_book()
    assert book_after.best_bid == 95.0
    assert book_after.best_ask == 105.0
    assert book_after.spread == 10.0
    assert book_after.is_crossed is False

    # No trades executed
    assert len(monitor.engine.get_trades()) == 0

    # UI snapshot reflects resting book without trades
    snapshot = monitor.render_text_snapshot()
    assert "$95.00" in snapshot
    assert "$105.00" in snapshot
    assert "No trades executed yet" in snapshot

    monitor.shutdown()


def test_rapid_burst_matching_and_persistence(temp_env):
    """Scenario 3: Multiple matched trades arriving rapidly under load."""
    db_path = temp_env["db"]
    monitor = LatencyMonitor(seed=42, enable_persistence=True, db_path=db_path)

    # Step through 200 orders rapidly
    latencies = monitor.step(num_orders=200)
    assert len(latencies) == 200
    assert monitor.engine.get_order_count() == 200

    # Wait briefly for persistence worker to commit
    time.sleep(0.2)
    trades = monitor.engine.get_trades()
    assert len(trades) > 0

    monitor.shutdown()

    # Verify trades were persisted into SQLite ledger
    repo = SQLiteTradeRepository(db_path=db_path)
    assert repo.get_trade_count() == len(trades)
    repo.close()


def test_malformed_and_incomplete_messages(temp_env):
    """Scenario 4: Malformed or incomplete binary messages."""
    mmap_path = temp_env["mmap"]
    buf = MmapRingBuffer(mmap_path, capacity=10, record_size=33, create=True)

    # 1. Truncated record (less than 33 bytes)
    with pytest.raises(ValueError, match="does not match buffer record size"):
        buf.write(b"\x00" * 20)

    # 2. Oversized record
    with pytest.raises(ValueError, match="does not match buffer record size"):
        buf.write(b"\x00" * 40)

    # 3. Corrupted payload unpacking
    processor = DataProcessor()
    with pytest.raises(ValueError, match="Invalid order size"):
        processor.unpack_order(b"short")

    buf.close()


def test_unexpected_consumer_termination_and_restart(temp_env):
    """Scenario 5: Consumer crashes and restarts, recovering unread trades."""
    mmap_path = temp_env["mmap"]
    producer = MmapRingBuffer(mmap_path, capacity=50, record_size=4, create=True)

    for i in range(10):
        producer.write(i.to_bytes(4, "little"))

    # Consumer 1 reads first 5 records, then abruptly terminates
    c1 = MmapRingBuffer(mmap_path, create=False)
    b1, seq1 = c1.read_batch(max_records=5)
    assert len(b1) == 5
    assert seq1 == 5
    c1.close()  # Simulated crash

    # Producer continues writing
    for i in range(10, 15):
        producer.write(i.to_bytes(4, "little"))

    # Consumer 2 boots up and resumes from sequence 5
    c2 = MmapRingBuffer(mmap_path, create=False)
    b2, seq2 = c2.read_batch(max_records=20, consumer_seq=seq1)
    assert len(b2) == 10  # records 5 to 14
    assert [int.from_bytes(b, "little") for b in b2] == list(range(5, 15))
    assert seq2 == 15

    c2.close()
    producer.close()


def test_missing_or_corrupted_mmap_handling(temp_env):
    """Scenario 6: Missing, inaccessible, or incorrectly formatted mmap data."""
    # Attaching to non-existent file
    with pytest.raises(MmapInaccessibleError):
        MmapRingBuffer("non_existent_file_987654.mmap", create=False)

    # Corrupted header file
    bad_path = temp_env["mmap"] + ".bad"
    with open(bad_path, "wb") as f:
        f.write(b"NOT_A_VALID_MAGIC_HEADER_RECORD_DATA_1234567890")

    with pytest.raises(MmapCorruptError):
        MmapRingBuffer(bad_path, create=False)

    if os.path.exists(bad_path):
        os.remove(bad_path)


def test_shutdown_with_pending_trades_no_data_loss(temp_env):
    """Scenario 7: Clean shutdown while trades remain pending guarantees no trade loss."""
    db_path = temp_env["db"]
    monitor = LatencyMonitor(enable_persistence=True, db_path=db_path)

    # Submit 10 matching pairs
    for i in range(1, 11):
        monitor.add_custom_order(order_id=i, side="BUY", price=100.0, quantity=10)
        monitor.add_custom_order(order_id=i + 100, side="SELL", price=100.0, quantity=10)

    trades_matched = len(monitor.engine.get_trades())
    assert trades_matched == 10

    # Shutdown immediately
    monitor.shutdown()

    # Database must contain all 10 matched trades
    repo = SQLiteTradeRepository(db_path=db_path)
    assert repo.get_trade_count() == 10
    repo.close()
