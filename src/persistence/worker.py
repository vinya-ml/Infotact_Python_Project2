"""Background Persistence Worker for Matched Trades (Member 3).

Asynchronously drains trades from the TradeEventQueue and persists them in batches
to the permanent database ledger without blocking the matching engine.

Features:
- Configurable batch sizing and flush intervals.
- A batch that fails to write is KEPT and retried (with backoff) until it
  succeeds, before any newer trade is taken, so trades are never skipped and
  stay in order. While the database is down, new trades wait in the queue;
  if the queue itself overflows, its drop counter records every loss.
- Graceful drain and shutdown: retries at shutdown are bounded so exit never
  hangs. Trades that still cannot be written are counted in `trades_abandoned`
  and logged; they are never lost silently.
- Metrics tracking: queue latency, write batch duration, retry/error counts.
- Isolated error handling: database locks or schema errors never crash the engine.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from src.ipc.trade_queue import TradeEventQueue, TradeEvent
from src.persistence.repository import BaseTradeRepository, TradeRecord

logger = logging.getLogger("chronosmatch.persistence")


class WorkerState(enum.Enum):
    """Lifecycle state of the persistence worker."""
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    FLUSHING = "FLUSHING"
    ERROR = "ERROR"


@dataclass
class PersistenceMetrics:
    """Operational statistics of background persistence."""
    worker_state: str = WorkerState.STOPPED.value
    trades_received: int = 0
    trades_persisted: int = 0
    duplicate_count: int = 0
    failed_write_count: int = 0
    batch_count: int = 0
    last_batch_size: int = 0
    last_write_latency_ns: int = 0
    last_queue_latency_ns: int = 0
    total_write_time_ns: int = 0
    pending_queue_size: int = 0
    retry_count: int = 0                # retry attempts made for batches that failed
    pending_retry_batch_size: int = 0   # trades currently held back waiting for a retry
    trades_abandoned: int = 0           # trades given up on at shutdown (database still down)

    @property
    def last_write_latency_ms(self) -> float:
        return self.last_write_latency_ns / 1_000_000.0

    @property
    def avg_write_latency_ms(self) -> float:
        if self.batch_count == 0:
            return 0.0
        return (self.total_write_time_ns / self.batch_count) / 1_000_000.0

    @property
    def last_queue_latency_us(self) -> float:
        return self.last_queue_latency_ns / 1_000.0


class BackgroundPersistenceWorker:
    """Worker daemon thread managing batched trade persistence."""

    def __init__(
        self,
        trade_queue: TradeEventQueue,
        repository: BaseTradeRepository,
        batch_size: int = 100,
        flush_interval_seconds: float = 0.05,
        auto_close_repo: bool = False,
        retry_initial_delay: float = 0.05,
        retry_max_delay: float = 2.0,
        shutdown_retry_attempts: int = 3,
    ) -> None:
        self.trade_queue = trade_queue
        self.repository = repository
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = max(0.001, float(flush_interval_seconds))
        self.auto_close_repo = auto_close_repo

        # Retry policy for a batch whose database write failed
        self._retry_initial_delay = max(0.001, float(retry_initial_delay))
        self._retry_max_delay = max(self._retry_initial_delay, float(retry_max_delay))
        self._retry_delay = self._retry_initial_delay
        self._shutdown_retry_attempts = max(1, int(shutdown_retry_attempts))
        self._pending_batch: list[TradeEvent] | None = None   # worker thread only

        self._state = WorkerState.STOPPED
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Operational metrics
        self._metrics = PersistenceMetrics()

    @property
    def state(self) -> WorkerState:
        with self._lock:
            return self._state

    def start(self) -> None:
        """Start the background persistence worker thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._state = WorkerState.RUNNING
            self._thread = threading.Thread(
                target=self._run_loop,
                name="ChronosMatch-PersistenceWorker",
                daemon=True,
            )
            self._thread.start()

    def _run_loop(self) -> None:
        """Main worker execution loop."""
        while not self._stop_event.is_set():
            try:
                if self._pending_batch is not None:
                    # An earlier write failed. Retry that exact batch (after a
                    # backoff pause) before taking anything newer.
                    if self._stop_event.wait(self._retry_delay):
                        break
                    with self._lock:
                        self._metrics.retry_count += 1
                    if not self._persist_batch(self._pending_batch):
                        self._retry_delay = min(self._retry_delay * 2, self._retry_max_delay)
                    continue

                # Pop batch with timeout
                batch = self.trade_queue.pop_batch(
                    max_items=self.batch_size,
                    timeout=self.flush_interval,
                )
                if batch:
                    self._persist_batch(batch)
            except Exception as exc:
                logger.error("Unexpected error in persistence worker loop: %s", exc, exc_info=True)
                with self._lock:
                    self._metrics.failed_write_count += 1
                time.sleep(0.05)

        # Drain remaining trades upon shutdown
        self._drain_remaining()

    def _persist_batch(self, batch: list[TradeEvent]) -> bool:
        """Convert events to records and commit to repository.

        Returns True when the batch is stored. On failure the batch is kept in
        `_pending_batch` so it is retried instead of lost.
        """
        if not batch:
            return True

        now_ns = time.perf_counter_ns()
        records: list[TradeRecord] = []
        for ev in batch:
            records.append(TradeRecord(
                trade_id=ev.trade_id,
                buy_order_id=ev.buy_order_id,
                sell_order_id=ev.sell_order_id,
                price=ev.price,
                quantity=ev.quantity,
                notional=ev.notional,
                timestamp_ns=ev.timestamp_ns,
                symbol=ev.symbol,
            ))

        # Latency spent in queue
        last_ev = batch[-1]
        queue_latency_ns = max(0, now_ns - last_ev.enqueue_time_ns)

        # Time the database write transaction separately from matching
        t0 = time.perf_counter_ns()
        try:
            inserted, duplicates = self.repository.save_trades_batch(records)
            t1 = time.perf_counter_ns()
            duration_ns = max(1, t1 - t0)

            with self._lock:
                self._metrics.trades_received += len(batch)
                self._metrics.trades_persisted += inserted
                self._metrics.duplicate_count += duplicates
                self._metrics.batch_count += 1
                self._metrics.last_batch_size = len(batch)
                self._metrics.last_write_latency_ns = duration_ns
                self._metrics.last_queue_latency_ns = queue_latency_ns
                self._metrics.total_write_time_ns += duration_ns
            self._pending_batch = None
            self._retry_delay = self._retry_initial_delay
            return True
        except Exception as exc:
            t1 = time.perf_counter_ns()
            logger.error("Failed to commit batch of %d trades (will retry): %s", len(batch), exc)
            with self._lock:
                self._metrics.failed_write_count += 1
                self._metrics.last_write_latency_ns = max(1, t1 - t0)
            self._pending_batch = batch
            return False

    def _drain_remaining(self) -> None:
        """Flush any pending trade events before thread exits."""
        with self._lock:
            self._state = WorkerState.FLUSHING

        abandoned_total = 0
        gave_up = False

        def write_with_bounded_retries(batch: list[TradeEvent]) -> bool:
            for attempt in range(self._shutdown_retry_attempts):
                if attempt:
                    time.sleep(self._retry_initial_delay)
                    with self._lock:
                        self._metrics.retry_count += 1
                if self._persist_batch(batch):
                    return True
            return False

        def abandon(batch: list[TradeEvent]) -> None:
            nonlocal abandoned_total
            abandoned_total += len(batch)
            with self._lock:
                self._metrics.trades_abandoned += len(batch)
            self._pending_batch = None

        # The batch that failed before shutdown goes first, to keep trade order.
        if self._pending_batch is not None:
            held = self._pending_batch
            if not write_with_bounded_retries(held):
                abandon(held)
                gave_up = True

        while True:
            batch = self.trade_queue.pop_batch(max_items=self.batch_size, timeout=0.0)
            if not batch:
                break
            if gave_up:
                abandon(batch)   # database is down: count the rest, do not stall the exit
            elif not write_with_bounded_retries(batch):
                abandon(batch)
                gave_up = True

        if abandoned_total:
            logger.error("Shutdown: %d trades could not be written to the database "
                         "and were abandoned (see trades_abandoned).", abandoned_total)

        with self._lock:
            self._state = WorkerState.ERROR if abandoned_total else WorkerState.STOPPED

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker to stop, drain all remaining trades, and wait for exit."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            self._thread = None

        if self.auto_close_repo:
            self.repository.close()

    def get_metrics(self) -> PersistenceMetrics:
        """Return snapshot of persistence performance and throughput."""
        with self._lock:
            self._metrics.worker_state = self._state.value
            self._metrics.pending_queue_size = self.trade_queue.size()
            return PersistenceMetrics(
                worker_state=self._metrics.worker_state,
                trades_received=self._metrics.trades_received,
                trades_persisted=self._metrics.trades_persisted,
                duplicate_count=self._metrics.duplicate_count,
                failed_write_count=self._metrics.failed_write_count,
                batch_count=self._metrics.batch_count,
                last_batch_size=self._metrics.last_batch_size,
                last_write_latency_ns=self._metrics.last_write_latency_ns,
                last_queue_latency_ns=self._metrics.last_queue_latency_ns,
                total_write_time_ns=self._metrics.total_write_time_ns,
                pending_queue_size=self._metrics.pending_queue_size,
                retry_count=self._metrics.retry_count,
                pending_retry_batch_size=len(self._pending_batch) if self._pending_batch else 0,
                trades_abandoned=self._metrics.trades_abandoned,
            )

    def is_alive(self) -> bool:
        """Return True if worker background thread is running."""
        return self._thread is not None and self._thread.is_alive()