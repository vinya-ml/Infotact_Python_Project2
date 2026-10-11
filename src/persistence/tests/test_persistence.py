"""Automated test suite for Background Persistence Layer & SQLite Ledger (Member 3)."""

import os
import sqlite3
import tempfile
import time
from unittest.mock import MagicMock
import pytest

from src.ipc.trade_queue import TradeEventQueue, TradeEvent
from src.persistence.repository import SQLiteTradeRepository, TradeRecord
from src.persistence.worker import BackgroundPersistenceWorker, WorkerState


@pytest.fixture
def temp_db_path():
    """Create a temporary SQLite database path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    if os.path.exists(path):
        os.remove(path)
    yield path
    # Cleanup DB and associated WAL / SHM files
    for ext in ("", "-wal", "-shm"):
        f = path + ext
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass


def test_sqlite_repository_schema_and_batch_insert(temp_db_path):
    """Verify table creation, indexes, and batch insertion in SQLite."""
    repo = SQLiteTradeRepository(db_path=temp_db_path)
    assert repo.get_trade_count() == 0

    trades = [
        TradeRecord(
            trade_id=f"TRD_{i}",
            buy_order_id=100 + i,
            sell_order_id=200 + i,
            price=101.50 + i,
            quantity=10 * i,
            notional=(101.50 + i) * (10 * i),
            timestamp_ns=time.perf_counter_ns(),
        )
        for i in range(1, 6)
    ]

    inserted, duplicates = repo.save_trades_batch(trades)
    assert inserted == 5
    assert duplicates == 0
    assert repo.get_trade_count() == 5

    # Query back
    records = repo.get_trades(limit=10)
    assert len(records) == 5
    assert records[0].trade_id == "TRD_1"
    assert records[0].price == 102.50
    assert records[0].quantity == 10

    # Aggregate stats
    stats = repo.query_aggregate_stats()
    assert stats["trade_count"] == 5
    assert stats["total_volume"] == sum(t.quantity for t in trades)

    repo.close()


def test_sqlite_repository_deduplication(temp_db_path):
    """Verify idempotent insertion: replayed trade events do not produce duplicate rows."""
    repo = SQLiteTradeRepository(db_path=temp_db_path)

    trade = TradeRecord(
        trade_id="UNIQUE_TRADE_001",
        buy_order_id=1,
        sell_order_id=2,
        price=99.0,
        quantity=50,
        notional=4950.0,
        timestamp_ns=1000,
    )

    # First insert
    ins1, dup1 = repo.save_trades_batch([trade])
    assert ins1 == 1
    assert dup1 == 0
    assert repo.get_trade_count() == 1

    # Re-insert the same trade (duplicate)
    ins2, dup2 = repo.save_trades_batch([trade])
    assert ins2 == 0
    assert dup2 == 1
    assert repo.get_trade_count() == 1  # Still 1, deduplicated

    repo.close()


def test_persistence_worker_lifecycle_and_batching(temp_db_path):
    """Test BackgroundPersistenceWorker startup, batch draining, and graceful shutdown."""
    queue = TradeEventQueue(capacity=500)
    repo = SQLiteTradeRepository(db_path=temp_db_path)
    worker = BackgroundPersistenceWorker(
        trade_queue=queue,
        repository=repo,
        batch_size=10,
        flush_interval_seconds=0.02,
        auto_close_repo=False,
    )

    assert worker.state == WorkerState.STOPPED
    worker.start()
    assert worker.state == WorkerState.RUNNING
    assert worker.is_alive() is True

    # Push 25 trades into queue
    for i in range(25):
        queue.push({
            "trade_id": f"T_{i}",
            "buy_id": i,
            "sell_id": i + 100,
            "price": 50.0,
            "qty": 5,
        })

    # Wait briefly for worker to batch persist
    time.sleep(0.15)

    metrics = worker.get_metrics()
    assert metrics.trades_persisted >= 20
    assert metrics.batch_count >= 2
    assert metrics.last_write_latency_ns > 0

    # Shut down cleanly, ensuring any remaining trades are flushed
    worker.stop(timeout=2.0)
    assert worker.state == WorkerState.STOPPED
    assert worker.is_alive() is False

    # All 25 trades must be in the database
    assert repo.get_trade_count() == 25
    repo.close()


def test_worker_shutdown_with_pending_trades(temp_db_path):
    """Verify shutdown guarantee: all pending trades are flushed even if worker is stopped immediately."""
    queue = TradeEventQueue(capacity=500)
    repo = SQLiteTradeRepository(db_path=temp_db_path)
    worker = BackgroundPersistenceWorker(
        trade_queue=queue,
        repository=repo,
        batch_size=50,
        flush_interval_seconds=1.0,  # Long flush interval
        auto_close_repo=False,
    )

    worker.start()

    # Rapidly inject 40 trades
    for i in range(40):
        queue.push({
            "trade_id": f"DRAIN_{i}",
            "buy_id": i,
            "sell_id": i + 1,
            "price": 10.0,
            "qty": 1,
        })

    # Immediately stop worker without waiting for normal timer
    worker.stop(timeout=3.0)

    # All 40 trades must have been drained and saved
    assert repo.get_trade_count() == 40
    repo.close()


def test_database_locked_retry_handling(temp_db_path):
    """Verify repository retry behavior on transient database busy conditions."""
    repo = SQLiteTradeRepository(db_path=temp_db_path, max_retries=3, retry_backoff=0.01)

    # Mock connection to simulate locked error once, then succeed
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    call_count = 0

    def mock_executemany(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise sqlite3.OperationalError("database is locked")
        return None

    mock_conn.executemany.side_effect = mock_executemany
    mock_conn.total_changes = 1

    repo._conn = mock_conn

    trade = TradeRecord("RETRY_TRD", 1, 2, 10.0, 1, 10.0, 100)
    inserted, _ = repo.save_trades_batch([trade])

    assert call_count == 2  # Retried once and succeeded
    repo._conn = None


def test_worker_error_isolation(temp_db_path):
    """Verify worker logs database errors without crashing the thread loop."""
    queue = TradeEventQueue(capacity=50)

    # Repository that raises an unhandled error
    mock_repo = MagicMock(spec=SQLiteTradeRepository)
    mock_repo.save_trades_batch.side_effect = RuntimeError("Disk full simulation")

    worker = BackgroundPersistenceWorker(
        trade_queue=queue,
        repository=mock_repo,
        batch_size=5,
        flush_interval_seconds=0.01,
    )
    worker.start()

    queue.push({"buy_id": 1, "sell_id": 2, "price": 10.0, "qty": 1})
    time.sleep(0.08)

    metrics = worker.get_metrics()
    assert metrics.failed_write_count > 0
    assert worker.is_alive() is True  # Worker didn't die!

    worker.stop()
