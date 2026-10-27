"""Tests for the persistence worker's retry behaviour (Week 4 resilience).

Guarantees under test:
  * A batch whose database write fails is KEPT and retried until it succeeds.
  * It is retried before any newer trade, so nothing is skipped or reordered.
  * Trades arriving while the database is down wait in the queue.
  * At shutdown, retries are bounded so exit never hangs, and every trade
    that still cannot be written is counted in `trades_abandoned`.
"""

import os
import sqlite3
import sys
import time

import pytest

# Make the project root importable so this file also runs on its own.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.ipc.trade_queue import TradeEvent, TradeEventQueue  # noqa: E402
from src.persistence.repository import SQLiteTradeRepository  # noqa: E402
from src.persistence.worker import BackgroundPersistenceWorker, WorkerState  # noqa: E402

FAST = dict(flush_interval_seconds=0.005, retry_initial_delay=0.005, retry_max_delay=0.02)


def make_event(i):
    return TradeEvent(trade_id=f"T{i}", buy_order_id=i, sell_order_id=i + 1000,
                      price=100.0, quantity=1, notional=100.0, timestamp_ns=i)


def push_trades(queue, start, count):
    for i in range(start, start + count):
        assert queue.push(make_event(i)) is True


def wait_until(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


class FlakyRepo:
    """Stand-in repository: fails the first `fail_times` writes, then works."""

    def __init__(self, fail_times=0, exc=None):
        self.fail_times = fail_times
        self.exc = exc or RuntimeError("disk full (simulated)")
        self.calls = 0
        self.saved_ids = []        # trade ids, in the order they were stored

    def save_trades_batch(self, records):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        self.saved_ids.extend(r.trade_id for r in records)
        return len(records), 0

    def close(self):
        pass


def ids(first, count):
    return [f"T{i}" for i in range(first, first + count)]


def test_failed_batch_is_retried_and_nothing_is_lost():
    queue, repo = TradeEventQueue(capacity=1000), FlakyRepo(fail_times=3)
    worker = BackgroundPersistenceWorker(queue, repo, batch_size=100, **FAST)
    worker.start()
    push_trades(queue, 0, 10)
    assert wait_until(lambda: len(repo.saved_ids) == 10)
    worker.stop()

    m = worker.get_metrics()
    assert repo.saved_ids == ids(0, 10)
    assert m.trades_persisted == 10
    assert m.failed_write_count == 3
    assert m.retry_count >= 3
    assert m.trades_abandoned == 0
    assert m.pending_retry_batch_size == 0


def test_worker_keeps_retrying_through_many_failures_while_running():
    queue, repo = TradeEventQueue(capacity=1000), FlakyRepo(fail_times=15)
    worker = BackgroundPersistenceWorker(queue, repo, batch_size=100, **FAST)
    worker.start()
    push_trades(queue, 0, 10)
    assert wait_until(lambda: len(repo.saved_ids) == 10)
    worker.stop()
    assert repo.saved_ids == ids(0, 10)
    assert worker.get_metrics().trades_abandoned == 0


def test_the_failed_batch_is_written_before_newer_trades():
    queue, repo = TradeEventQueue(capacity=1000), FlakyRepo(fail_times=2)
    worker = BackgroundPersistenceWorker(queue, repo, batch_size=5, **FAST)
    worker.start()
    push_trades(queue, 0, 5)
    assert wait_until(lambda: repo.calls >= 1)     # the first batch has failed
    push_trades(queue, 5, 5)                       # newer trades arrive meanwhile
    assert wait_until(lambda: len(repo.saved_ids) == 10)
    worker.stop()
    assert repo.saved_ids == ids(0, 10)            # original order preserved


def test_trades_arriving_while_the_database_is_down_are_kept():
    queue, repo = TradeEventQueue(capacity=1000), FlakyRepo(fail_times=6)
    worker = BackgroundPersistenceWorker(queue, repo, batch_size=10, **FAST)
    worker.start()
    push_trades(queue, 0, 5)
    assert wait_until(lambda: repo.calls >= 2)     # database is down right now
    push_trades(queue, 5, 20)
    assert wait_until(lambda: len(repo.saved_ids) == 25)
    worker.stop()
    assert repo.saved_ids == ids(0, 25)
    assert queue.stats()["dropped_count"] == 0


def test_pending_batch_is_visible_in_metrics_while_the_database_is_down():
    queue, repo = TradeEventQueue(capacity=1000), FlakyRepo(fail_times=10 ** 9)
    worker = BackgroundPersistenceWorker(queue, repo, batch_size=100,
                                         flush_interval_seconds=0.005,
                                         retry_initial_delay=0.01, retry_max_delay=0.02)
    worker.start()
    push_trades(queue, 0, 7)
    assert wait_until(lambda: worker.get_metrics().pending_retry_batch_size == 7)
    worker.stop()


def test_shutdown_with_the_database_down_does_not_hang_and_counts_every_trade():
    queue, repo = TradeEventQueue(capacity=1000), FlakyRepo(fail_times=10 ** 9)
    worker = BackgroundPersistenceWorker(queue, repo, batch_size=100, **FAST)
    worker.start()
    push_trades(queue, 0, 250)                     # 100 held for retry + 150 still queued
    assert wait_until(lambda: repo.calls >= 1)

    started = time.time()
    worker.stop(timeout=10)
    elapsed = time.time() - started

    m = worker.get_metrics()
    assert not worker.is_alive()
    assert elapsed < 3.0                           # bounded: exit does not stall
    assert m.trades_persisted == 0
    assert m.trades_abandoned == 250               # nothing lost silently
    assert repo.saved_ids == []
    assert worker.state == WorkerState.ERROR


def test_shutdown_while_the_database_recovers_loses_nothing():
    queue, repo = TradeEventQueue(capacity=1000), FlakyRepo(fail_times=2)
    # Long retry delay: the worker is still waiting to retry when stop() arrives,
    # so the shutdown drain is what has to finish the job.
    worker = BackgroundPersistenceWorker(queue, repo, batch_size=100,
                                         flush_interval_seconds=0.005,
                                         retry_initial_delay=0.2, retry_max_delay=0.2)
    worker.start()
    push_trades(queue, 0, 10)
    assert wait_until(lambda: repo.calls >= 1)
    worker.stop(timeout=10)

    m = worker.get_metrics()
    assert repo.saved_ids == ids(0, 10)
    assert m.trades_abandoned == 0
    assert worker.state == WorkerState.STOPPED


def test_with_no_failures_nothing_changes():
    queue, repo = TradeEventQueue(capacity=1000), FlakyRepo(fail_times=0)
    worker = BackgroundPersistenceWorker(queue, repo, batch_size=100, **FAST)
    worker.start()
    push_trades(queue, 0, 30)
    assert wait_until(lambda: len(repo.saved_ids) == 30)
    worker.stop()
    m = worker.get_metrics()
    assert (m.failed_write_count, m.retry_count, m.trades_abandoned) == (0, 0, 0)
    assert m.trades_persisted == 30
    assert worker.state == WorkerState.STOPPED


class _FailingConnection:
    """Wraps a real sqlite3 connection; executemany fails `times` times first."""

    def __init__(self, real, exc, times):
        self._real, self._exc, self._left = real, exc, times

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *args):
        return self._real.__exit__(*args)

    def executemany(self, *args):
        if self._left > 0:
            self._left -= 1
            raise self._exc
        return self._real.executemany(*args)


@pytest.mark.parametrize("label, exc, times", [
    ("three 'locked' errors in a row (more than the repository retries)",
     sqlite3.OperationalError("database is locked"), 3),
    ("a disk I/O error", sqlite3.OperationalError("disk I/O error"), 1),
    ("an OSError (disk full)", OSError("No space left on device"), 1),
])
def test_real_sqlite_ledger_survives_hard_write_failures(tmp_path, label, exc, times):
    repo = SQLiteTradeRepository(str(tmp_path / "ledger.db"), retry_backoff=0.001)
    repo.initialize()
    proxy = _FailingConnection(repo._get_connection(), exc, times)
    repo._get_connection = lambda: proxy

    queue = TradeEventQueue(capacity=1000)
    worker = BackgroundPersistenceWorker(queue, repo, batch_size=100, **FAST)
    worker.start()
    push_trades(queue, 0, 10)
    assert wait_until(lambda: repo.get_trade_count() == 10), label
    worker.stop()
    assert repo.get_trade_count() == 10
    assert worker.get_metrics().trades_abandoned == 0