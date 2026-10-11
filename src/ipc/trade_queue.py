"""Thread-Safe Bounded Trade Event Queue for Latency-Decoupled IPC (Member 3).

Decouples the microsecond matching engine from downstream consumers
(background database persistence worker, live UI tap, WebSocket streaming).

Features:
- Bounded capacity with configurable backpressure strategies (DROP_OLDEST, DROP_NEWEST, BLOCK, RAISE).
- High-throughput batch polling (`pop_batch`) with timeout.
- Nanosecond queue dwell latency measurement.
- Real-time subscriber listener dispatch without blocking the main engine path.
- Observable health and queue metrics.
"""

from __future__ import annotations

import collections
import enum
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


class BackpressurePolicy(enum.Enum):
    """Behavior when TradeEventQueue reaches its maximum capacity."""
    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"
    BLOCK = "block"
    RAISE = "raise"


class QueueFullError(Exception):
    """Raised when queue is full and policy is set to RAISE."""


@dataclass
class TradeEvent:
    """Standardized matched trade event produced by ChronosMatch engine."""
    trade_id: str
    buy_order_id: int
    sell_order_id: int
    price: float
    quantity: int
    notional: float
    timestamp_ns: int
    enqueue_time_ns: int = field(default_factory=time.perf_counter_ns)
    symbol: str = "DEFAULT"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_engine_dict(cls, data: dict[str, Any], sequence: int = 0) -> TradeEvent:
        """Construct a standardized TradeEvent from Member 2's trade dict format."""
        buy_id = int(data.get("buy_id", 0))
        sell_id = int(data.get("sell_id", 0))
        price = float(data.get("price", 0.0))
        qty = int(data.get("qty", data.get("quantity", 0)))
        notional = float(data.get("notional", price * qty))
        ts = int(data.get("timestamp_ns", data.get("timestamp", time.perf_counter_ns())))

        # Stable deduplication key
        trade_id = str(data.get("trade_id") or f"TRD_{buy_id}_{sell_id}_{ts}_{sequence}")

        return cls(
            trade_id=trade_id,
            buy_order_id=buy_id,
            sell_order_id=sell_id,
            price=price,
            quantity=qty,
            notional=notional,
            timestamp_ns=ts,
            symbol=str(data.get("symbol", "DEFAULT")),
        )


class TradeEventQueue:
    """High-throughput, bounded, thread-safe trade queue."""

    def __init__(
        self,
        capacity: int = 50000,
        backpressure: BackpressurePolicy = BackpressurePolicy.DROP_OLDEST,
    ) -> None:
        self.capacity = max(1, int(capacity))
        self.backpressure = backpressure

        self._queue: collections.deque[TradeEvent] = collections.deque()
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._not_full = threading.Condition(self._lock)

        self._subscribers: list[Callable[[TradeEvent], None]] = []
        self._closed = False
        self._sequence = 0

        # Metrics
        self._total_pushed = 0
        self._total_popped = 0
        self._dropped_count = 0
        self._high_watermark = 0
        self._total_queue_delay_ns = 0

    def subscribe(self, callback: Callable[[TradeEvent], None]) -> None:
        """Register a subscriber callback for real-time trade event tap."""
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[TradeEvent], None]) -> None:
        """Unregister a subscriber callback."""
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def push(self, trade: TradeEvent | dict[str, Any], timeout: float = 0.5) -> bool:
        """Push a trade event into the queue.

        Ultra-low-latency on the critical path.
        """
        if isinstance(trade, dict):
            with self._lock:
                self._sequence += 1
                seq = self._sequence
            event = TradeEvent.from_engine_dict(trade, sequence=seq)
        else:
            event = trade

        with self._lock:
            if self._closed:
                return False

            # Handle backpressure if capacity reached
            if len(self._queue) >= self.capacity:
                if self.backpressure == BackpressurePolicy.DROP_OLDEST:
                    self._queue.popleft()
                    self._dropped_count += 1
                elif self.backpressure == BackpressurePolicy.DROP_NEWEST:
                    self._dropped_count += 1
                    return False
                elif self.backpressure == BackpressurePolicy.RAISE:
                    self._dropped_count += 1
                    raise QueueFullError(f"TradeEventQueue saturated at capacity {self.capacity}")
                elif self.backpressure == BackpressurePolicy.BLOCK:
                    deadline = time.perf_counter() + timeout
                    while len(self._queue) >= self.capacity and not self._closed:
                        remaining = deadline - time.perf_counter()
                        if remaining <= 0:
                            self._dropped_count += 1
                            return False
                        self._not_full.wait(timeout=remaining)
                    if self._closed:
                        return False

            self._queue.append(event)
            self._total_pushed += 1
            curr_len = len(self._queue)
            if curr_len > self._high_watermark:
                self._high_watermark = curr_len

            self._not_empty.notify()

        # Fan-out to lightweight subscribers without holding the lock
        for sub in list(self._subscribers):
            try:
                sub(event)
            except Exception:
                pass

        return True

    def pop_batch(self, max_items: int = 100, timeout: float = 0.05) -> list[TradeEvent]:
        """Pop up to `max_items` trade events in a batch, waiting up to `timeout` seconds."""
        batch: list[TradeEvent] = []
        now_ns = time.perf_counter_ns()

        with self._lock:
            if not self._queue and not self._closed and timeout > 0:
                self._not_empty.wait(timeout=timeout)

            count = min(max_items, len(self._queue))
            for _ in range(count):
                event = self._queue.popleft()
                batch.append(event)
                delay_ns = max(0, now_ns - event.enqueue_time_ns)
                self._total_queue_delay_ns += delay_ns

            self._total_popped += len(batch)
            if batch:
                self._not_full.notify_all()

        return batch

    def pop_nowait(self) -> TradeEvent | None:
        """Immediately pop a single trade event without waiting."""
        with self._lock:
            if not self._queue:
                return None
            event = self._queue.popleft()
            self._total_popped += 1
            delay_ns = max(0, time.perf_counter_ns() - event.enqueue_time_ns)
            self._total_queue_delay_ns += delay_ns
            self._not_full.notify()
            return event

    def size(self) -> int:
        """Return current number of items in queue."""
        with self._lock:
            return len(self._queue)

    def is_empty(self) -> bool:
        """Return True if queue is currently empty."""
        with self._lock:
            return len(self._queue) == 0

    def stats(self) -> dict[str, Any]:
        """Return operational queue metrics."""
        with self._lock:
            curr_size = len(self._queue)
            popped = self._total_popped
            avg_delay_us = (self._total_queue_delay_ns / popped / 1000.0) if popped > 0 else 0.0
            return {
                "size": curr_size,
                "capacity": self.capacity,
                "utilization_pct": (curr_size / self.capacity) * 100.0,
                "total_pushed": self._total_pushed,
                "total_popped": popped,
                "dropped_count": self._dropped_count,
                "high_watermark": self._high_watermark,
                "avg_queue_delay_us": avg_delay_us,
                "is_closed": self._closed,
            }

    def close(self) -> None:
        """Close the queue, waking up any waiting consumers."""
        with self._lock:
            self._closed = True
            self._not_empty.notify_all()
            self._not_full.notify_all()

    def clear(self) -> None:
        """Clear all pending items from queue."""
        with self._lock:
            self._queue.clear()
            self._not_full.notify_all()
