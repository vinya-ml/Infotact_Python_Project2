"""ChronosMatch Latency Dashboard & Order Book Monitor (Member 3).

Responsibilities:
- Real-time curses terminal UI visualizing Top of Order Book and Bid/Ask spread.
- Sub-microsecond matching latency metrics (min, mean, p50, p95, p99, sparkline).
- Decoupled background persistence worker metrics (status, persisted count, pending queue, DB write latency).
- Dedicated Whale-Order & Large-Sweep detection and visualization panel (configurable threshold).
- Graceful lifecycle management, terminal resizing resilience, and headless execution.

Integrates with:
- Member 1's DataProcessor / binary IPC format
- Member 2's MatchingEngine Price-Time Priority order book
- Member 3's MmapRingBuffer, TradeEventQueue, and SQLiteTradeRepository
"""

from __future__ import annotations

import argparse
import collections
import curses
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from src.ipc.trade_queue import TradeEventQueue, TradeEvent, BackpressurePolicy
from src.persistence.repository import SQLiteTradeRepository, BaseTradeRepository, TradeRecord
from src.persistence.worker import BackgroundPersistenceWorker, WorkerState, PersistenceMetrics

# Side constants matching SCHEMA.md & matching_engine.pyx
BUY = 0
SELL = 1


# ---------------------------------------------------------------------------
# MatchingEngine Import / High-Performance Fallback
# ---------------------------------------------------------------------------

try:
    from src.engine.matching_engine import MatchingEngine, BUY as ENGINE_BUY, SELL as ENGINE_SELL, HAS_CYTHON_ENGINE  # type: ignore
except (ImportError, ModuleNotFoundError):
    # Pure-Python fallback matching the exact SCHEMA.md & matching_engine.pyx contract
    HAS_CYTHON_ENGINE = False

    class _PyCOrderObj:
        __slots__ = ("order_id", "side", "price", "quantity", "timestamp")

        def __init__(self, order_id: int, side: int, price: float, quantity: int, timestamp: int):
            self.order_id = int(order_id)
            self.side = int(side)
            self.price = float(price)
            self.quantity = int(quantity)
            self.timestamp = int(timestamp)

    class MatchingEngine:  # type: ignore[no-redef]
        """Pure-Python reference matching engine with exact Cython interface."""

        def __init__(self):
            self.order_count: int = 0
            self.buy_orders: list[_PyCOrderObj] = []
            self.sell_orders: list[_PyCOrderObj] = []
            self.trades: list[dict[str, Any]] = []

        def add_order(self, order_id: int, side: int, price: float, quantity: int, timestamp: int) -> int:
            self.order_count += 1
            wrapped = _PyCOrderObj(order_id, side, price, quantity, timestamp)
            if side == BUY:
                self._match_buy(wrapped)
            else:
                self._match_sell(wrapped)
            return int(order_id)

        def _match_buy(self, order: _PyCOrderObj) -> None:
            self.sell_orders.sort(key=lambda o: (o.price, o.timestamp))
            while order.quantity > 0 and self.sell_orders and self.sell_orders[0].price <= order.price:
                best_sell = self.sell_orders[0]
                matched_qty = min(order.quantity, best_sell.quantity)
                self.trades.append({
                    "buy_id": order.order_id,
                    "sell_id": best_sell.order_id,
                    "price": best_sell.price,
                    "qty": matched_qty,
                })
                order.quantity -= matched_qty
                best_sell.quantity -= matched_qty
                if best_sell.quantity == 0:
                    self.sell_orders.pop(0)

            if order.quantity > 0:
                self.buy_orders.append(order)

        def _match_sell(self, order: _PyCOrderObj) -> None:
            self.buy_orders.sort(key=lambda o: (-o.price, o.timestamp))
            while order.quantity > 0 and self.buy_orders and self.buy_orders[0].price >= order.price:
                best_buy = self.buy_orders[0]
                matched_qty = min(order.quantity, best_buy.quantity)
                self.trades.append({
                    "buy_id": best_buy.order_id,
                    "sell_id": order.order_id,
                    "price": best_buy.price,
                    "qty": matched_qty,
                })
                order.quantity -= matched_qty
                best_buy.quantity -= matched_qty
                if best_buy.quantity == 0:
                    self.buy_orders.pop(0)

            if order.quantity > 0:
                self.sell_orders.append(order)

        def get_order_count(self) -> int:
            return self.order_count

        def get_trades(self) -> list[dict[str, Any]]:
            return self.trades

        def get_order_book_snapshot(self) -> list[float | None]:
            best_bid = self.buy_orders[0].price if self.buy_orders else None
            best_ask = self.sell_orders[0].price if self.sell_orders else None
            return [best_bid, best_ask]


def get_matching_engine() -> tuple[MatchingEngine, str]:
    """Factory creating an engine instance and reporting whether Cython or Python is active."""
    engine = MatchingEngine()
    engine_name = "Cython (Native C)" if HAS_CYTHON_ENGINE else "Python Engine (Reference)"
    return engine, engine_name


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class WhaleEvent:
    """Represents a detected whale order or large executed trade sweep."""
    event_type: str  # "RESTING_ORDER" or "MATCHED_TRADE"
    side: str        # "BUY", "SELL", or "TRADE"
    price: float
    quantity: int
    notional: float
    order_id: int | None = None
    buy_id: int | None = None
    sell_id: int | None = None
    timestamp_ns: int = 0


@dataclass
class OrderBookTop:
    """Snapshot of top-of-book, bid/ask spread, and resting depth levels."""
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    spread_bps: float | None = None
    mid_price: float | None = None
    bid_levels: list[tuple[float, int]] = field(default_factory=list)  # (price, total_qty)
    ask_levels: list[tuple[float, int]] = field(default_factory=list)  # (price, total_qty)
    total_bid_qty: int = 0
    total_ask_qty: int = 0
    bid_order_count: int = 0
    ask_order_count: int = 0
    is_crossed: bool = False


@dataclass
class LatencyStats:
    """Latency percentiles and throughput metrics."""
    count: int = 0
    last_ns: int = 0
    min_ns: int = 0
    max_ns: int = 0
    mean_ns: float = 0.0
    p50_ns: float = 0.0
    p95_ns: float = 0.0
    p99_ns: float = 0.0
    throughput_ops: float = 0.0
    rolling_mean_ns: float = 0.0
    sparkline: str = ""

    @property
    def last_us(self) -> float:
        return self.last_ns / 1000.0

    @property
    def min_us(self) -> float:
        return self.min_ns / 1000.0

    @property
    def max_us(self) -> float:
        return self.max_ns / 1000.0

    @property
    def mean_us(self) -> float:
        return self.mean_ns / 1000.0

    @property
    def p50_us(self) -> float:
        return self.p50_ns / 1000.0

    @property
    def p95_us(self) -> float:
        return self.p95_ns / 1000.0

    @property
    def p99_us(self) -> float:
        return self.p99_ns / 1000.0

    @property
    def rolling_mean_us(self) -> float:
        return self.rolling_mean_ns / 1000.0


@dataclass
class PersistenceUIStats:
    """Persistence worker health and throughput metrics."""
    worker_state: str = "DISABLED"
    trades_persisted: int = 0
    pending_queue_size: int = 0
    failed_writes: int = 0
    batch_count: int = 0
    last_write_latency_ms: float = 0.0
    avg_write_latency_ms: float = 0.0
    last_queue_latency_us: float = 0.0


@dataclass
class MarketState:
    """Aggregated market overview for real-time reporting."""
    orders_count: int = 0
    trades_count: int = 0
    total_volume: int = 0
    total_notional: float = 0.0
    vwap: float | None = None
    recent_trades: list[dict[str, Any]] = field(default_factory=list)
    recent_whale_events: list[WhaleEvent] = field(default_factory=list)
    last_order: dict[str, Any] | None = None
    engine_name: str = "Unknown"
    is_paused: bool = False
    rate_target: int = 30
    elapsed_seconds: float = 0.0
    persistence_stats: PersistenceUIStats = field(default_factory=PersistenceUIStats)
    active_whale_threshold: int = 500


# ---------------------------------------------------------------------------
# Utility Helpers
# ---------------------------------------------------------------------------

def calculate_percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear interpolation percentile (0 to 100)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pct = max(0.0, min(100.0, pct))
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return float(sorted_values[low] * (1.0 - frac) + sorted_values[high] * frac)


def generate_sparkline(values: Sequence[float], width: int = 18, ascii_only: bool = True) -> str:
    """Generate an ASCII/Unicode sparkline from numeric values."""
    if not values:
        return "." * width
    recent = list(values)[-width:]
    if ascii_only or sys.platform == "win32":
        bars = [".", "_", "-", "=", "+", "*", "#"]
    else:
        bars = [" ", " ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    lo, hi = min(recent), max(recent)
    if hi == lo:
        return bars[0] * len(recent)
    result = []
    for v in recent:
        norm = (v - lo) / (hi - lo)
        idx = min(int(norm * (len(bars) - 1)), len(bars) - 1)
        result.append(bars[idx])
    return "".join(result)


# ---------------------------------------------------------------------------
# LatencyMonitor
# ---------------------------------------------------------------------------

class LatencyMonitor:
    """Coordinates matching engine execution, feeds, metrics, and persistence."""

    def __init__(
        self,
        engine: Any | None = None,
        seed: int = 42,
        base_price: float = 100.0,
        whale_threshold: int = 500,
        whale_mode: str = "absolute",
        rolling_window: int = 100,
        enable_persistence: bool = False,
        db_path: str = "chronosmatch_trades.db",
        queue_capacity: int = 50000,
    ):
        if engine is not None:
            self.engine = engine
            self.engine_name = type(engine).__name__
        else:
            self.engine, self.engine_name = get_matching_engine()

        self.seed = seed
        self.base_price = float(base_price)
        self.whale_threshold = int(whale_threshold)
        self.whale_mode = str(whale_mode).lower()  # "absolute" or "relative"
        self.rolling_window = int(rolling_window)
        self.enable_persistence = bool(enable_persistence)
        self.db_path = db_path
        self.queue_capacity = queue_capacity

        # Simulator feed
        from src.analysis.simulator import MockMarketSimulator
        self.simulator = MockMarketSimulator(seed=self.seed, base_price=self.base_price)

        # Latency collection
        self.all_latencies_ns: list[int] = []
        self.rolling_latencies_ns: collections.deque[int] = collections.deque(maxlen=self.rolling_window)
        self.rolling_order_quantities: collections.deque[int] = collections.deque(maxlen=200)
        self._start_time_ns: int = time.perf_counter_ns()
        self._last_order_info: dict[str, Any] | None = None
        self._last_latency_ns: int = 0
        self._last_processed_trade_idx = 0

        # Whale tracking
        self.whale_events: collections.deque[WhaleEvent] = collections.deque(maxlen=50)

        # Background Persistence components
        self.trade_queue: TradeEventQueue | None = None
        self.repository: BaseTradeRepository | None = None
        self.worker: BackgroundPersistenceWorker | None = None

        if self.enable_persistence:
            self._init_persistence()

    def _init_persistence(self) -> None:
        """Initialize and start background persistence worker."""
        self.trade_queue = TradeEventQueue(
            capacity=self.queue_capacity,
            backpressure=BackpressurePolicy.DROP_OLDEST,
        )
        self.repository = SQLiteTradeRepository(db_path=self.db_path)
        self.worker = BackgroundPersistenceWorker(
            trade_queue=self.trade_queue,
            repository=self.repository,
            batch_size=100,
            flush_interval_seconds=0.05,
            auto_close_repo=True,
        )
        self.worker.start()

    def get_current_whale_threshold(self) -> int:
        """Return active whale threshold, adapting dynamically if in relative mode."""
        if self.whale_mode == "relative" and len(self.rolling_order_quantities) >= 20:
            # 95th percentile of recent order quantities, floor at 100
            q95 = calculate_percentile(sorted(self.rolling_order_quantities), 95)
            return max(100, int(q95))
        return self.whale_threshold

    def reset(self, new_seed: int | None = None) -> None:
        """Reset the monitor, clearing engine state, orders, and collected latencies."""
        self.engine, self.engine_name = get_matching_engine()
        if new_seed is not None:
            self.seed = new_seed
        self.simulator.reset(seed=self.seed)
        self.all_latencies_ns.clear()
        self.rolling_latencies_ns.clear()
        self.rolling_order_quantities.clear()
        self._start_time_ns = time.perf_counter_ns()
        self._last_order_info = None
        self._last_latency_ns = 0
        self._last_processed_trade_idx = 0
        self.whale_events.clear()

        if self.trade_queue is not None:
            self.trade_queue.clear()

    def _on_order_submitted(self, order_id: int, side: str, price: float, quantity: int, ts_ns: int) -> None:
        """Track order size distribution and inspect for resting whale orders."""
        self.rolling_order_quantities.append(quantity)
        thresh = self.get_current_whale_threshold()
        if quantity >= thresh:
            self.whale_events.append(WhaleEvent(
                event_type="RESTING_ORDER",
                side=side,
                price=price,
                quantity=quantity,
                notional=price * quantity,
                order_id=order_id,
                timestamp_ns=ts_ns,
            ))

    def _sync_matched_trades_to_persistence(self) -> None:
        """Enqueue any newly executed trades into the persistence queue and check for whale sweeps."""
        trades = self.engine.get_trades()
        total_trades = len(trades)
        if total_trades > self._last_processed_trade_idx:
            new_trades = trades[self._last_processed_trade_idx:total_trades]
            thresh = self.get_current_whale_threshold()

            for tr in new_trades:
                qty = int(tr.get("qty", tr.get("quantity", 0)))
                p = float(tr.get("price", 0.0))

                # Check if this matched trade is a whale sweep
                if qty >= thresh:
                    self.whale_events.append(WhaleEvent(
                        event_type="MATCHED_TRADE",
                        side="TRADE",
                        price=p,
                        quantity=qty,
                        notional=p * qty,
                        buy_id=tr.get("buy_id"),
                        sell_id=tr.get("sell_id"),
                        timestamp_ns=time.perf_counter_ns(),
                    ))

                # Push to background persistence worker
                if self.enable_persistence and self.trade_queue is not None:
                    self.trade_queue.push(tr)

            self._last_processed_trade_idx = total_trades

    def step(self, num_orders: int = 1) -> list[int]:
        """Generate and match next order(s), recording precise dispatch latency."""
        latencies = []
        for _ in range(max(1, num_orders)):
            order = self.simulator._next_order(whale_prob=0.04)
            side_val = BUY if order["side"] == "BUY" else SELL
            price_val = float(order["price"])
            qty_val = int(order["quantity"])
            ts_val = time.perf_counter_ns()
            order_id = int(order["order_id"])

            self._last_order_info = {
                "order_id": order_id,
                "side": order["side"],
                "price": price_val,
                "quantity": qty_val,
                "timestamp_ns": ts_val,
            }

            self._on_order_submitted(order_id, order["side"], price_val, qty_val, ts_val)

            # Measure order dispatch latency to matching engine in nanoseconds
            t0 = time.perf_counter_ns()
            self.engine.add_order(order_id, side_val, price_val, qty_val, ts_val)
            t1 = time.perf_counter_ns()

            lat = max(1, t1 - t0)
            self._last_latency_ns = lat
            self.all_latencies_ns.append(lat)
            self.rolling_latencies_ns.append(lat)
            latencies.append(lat)

        self._sync_matched_trades_to_persistence()
        return latencies

    def add_custom_order(
        self,
        order_id: int,
        side: str | int,
        price: float,
        quantity: int,
        timestamp_ns: int | None = None,
    ) -> int:
        """Manually submit an order to the matching engine and track latency."""
        if timestamp_ns is None:
            timestamp_ns = time.perf_counter_ns()

        if isinstance(side, str):
            side_val = BUY if side.upper() == "BUY" else SELL
            side_str = side.upper()
        else:
            side_val = BUY if side == BUY else SELL
            side_str = "BUY" if side_val == BUY else "SELL"

        self._last_order_info = {
            "order_id": order_id,
            "side": side_str,
            "price": float(price),
            "quantity": int(quantity),
            "timestamp_ns": timestamp_ns,
        }

        self._on_order_submitted(order_id, side_str, float(price), int(quantity), timestamp_ns)

        t0 = time.perf_counter_ns()
        res = self.engine.add_order(order_id, side_val, float(price), int(quantity), timestamp_ns)
        t1 = time.perf_counter_ns()

        lat = max(1, t1 - t0)
        self._last_latency_ns = lat
        self.all_latencies_ns.append(lat)
        self.rolling_latencies_ns.append(lat)

        self._sync_matched_trades_to_persistence()
        return res


    def get_top_of_book(self, depth: int = 5) -> OrderBookTop:
        """Return best prices, aggregated depth and resting order counts."""
        if depth < 0:
            raise ValueError("depth must be non-negative")

        raw_snapshot = self.engine.get_order_book_snapshot()
        raw_bid, raw_ask = (
            (raw_snapshot[0], raw_snapshot[1])
            if raw_snapshot
            else (None, None)
        )

        if hasattr(self.engine, "get_order_book_depth"):
            # Cython engine exposes book data through public methods.
            bid_levels, ask_levels = self.engine.get_order_book_depth(depth)
            bid_levels = list(bid_levels)
            ask_levels = list(ask_levels)

            bid_count, ask_count = self.engine.get_order_book_counts()
            bid_order_count = int(bid_count)
            ask_order_count = int(ask_count)

            # Request enough levels to calculate full-book quantities.
            all_bids, all_asks = self.engine.get_order_book_depth(
                max(depth, bid_order_count + ask_order_count)
            )
            total_bid_qty = sum(int(qty) for _, qty in all_bids)
            total_ask_qty = sum(int(qty) for _, qty in all_asks)
        else:
            # Python fallback engine.
            buys = list(getattr(self.engine, "buy_orders", []))
            sells = list(getattr(self.engine, "sell_orders", []))

            bid_map = collections.defaultdict(int)
            ask_map = collections.defaultdict(int)

            for order in buys:
                qty = getattr(order, "quantity", getattr(order, "qty", 0))
                bid_map[float(order.price)] += int(qty)

            for order in sells:
                qty = getattr(order, "quantity", getattr(order, "qty", 0))
                ask_map[float(order.price)] += int(qty)

            all_bids = sorted(
                bid_map.items(), key=lambda item: -item[0]
            )
            all_asks = sorted(
                ask_map.items(), key=lambda item: item[0]
            )

            bid_levels = all_bids[:depth]
            ask_levels = all_asks[:depth]
            bid_order_count = len(buys)
            ask_order_count = len(sells)
            total_bid_qty = sum(bid_map.values())
            total_ask_qty = sum(ask_map.values())

        best_bid = raw_bid
        best_ask = raw_ask
        spread = None
        spread_bps = None
        mid_price = None
        is_crossed = False

        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid
            mid_price = (best_bid + best_ask) / 2.0

            if mid_price > 0:
                spread_bps = spread / mid_price * 10000.0

            is_crossed = best_bid >= best_ask

        return OrderBookTop(
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            spread_bps=spread_bps,
            mid_price=mid_price,
            bid_levels=bid_levels,
            ask_levels=ask_levels,
            total_bid_qty=total_bid_qty,
            total_ask_qty=total_ask_qty,
            bid_order_count=bid_order_count,
            ask_order_count=ask_order_count,
            is_crossed=is_crossed,
        )


    def get_latency_stats(self) -> LatencyStats:
        """Calculate latency distribution (ns & us) and throughput."""
        n = len(self.all_latencies_ns)
        if n == 0:
            return LatencyStats(sparkline="·" * 15)

        sorted_lat = sorted(self.all_latencies_ns)
        elapsed_s = max(1e-9, (time.perf_counter_ns() - self._start_time_ns) / 1e9)
        throughput = n / elapsed_s

        rolling_mean = sum(self.rolling_latencies_ns) / len(self.rolling_latencies_ns) if self.rolling_latencies_ns else 0.0

        return LatencyStats(
            count=n,
            last_ns=self._last_latency_ns,
            min_ns=sorted_lat[0],
            max_ns=sorted_lat[-1],
            mean_ns=sum(sorted_lat) / n,
            p50_ns=calculate_percentile(sorted_lat, 50),
            p95_ns=calculate_percentile(sorted_lat, 95),
            p99_ns=calculate_percentile(sorted_lat, 99),
            throughput_ops=throughput,
            rolling_mean_ns=rolling_mean,
            sparkline=generate_sparkline([x / 1000.0 for x in self.rolling_latencies_ns], width=18),
        )

    def get_persistence_ui_stats(self) -> PersistenceUIStats:
        """Fetch current status and metrics from background persistence worker."""
        if not self.enable_persistence or self.worker is None:
            return PersistenceUIStats(worker_state="OFF")

        m = self.worker.get_metrics()
        return PersistenceUIStats(
            worker_state=m.worker_state,
            trades_persisted=m.trades_persisted,
            pending_queue_size=m.pending_queue_size,
            failed_writes=m.failed_write_count,
            batch_count=m.batch_count,
            last_write_latency_ms=m.last_write_latency_ms,
            avg_write_latency_ms=m.avg_write_latency_ms,
            last_queue_latency_us=m.last_queue_latency_us,
        )

    def get_market_state(self, is_paused: bool = False, rate_target: int = 30) -> MarketState:
        """Compile comprehensive market summary."""
        trades = self.engine.get_trades()
        total_vol = sum(int(t.get("qty", t.get("quantity", 0))) for t in trades)
        total_notional = sum(float(t.get("price", 0.0)) * int(t.get("qty", t.get("quantity", 0))) for t in trades)
        vwap = (total_notional / total_vol) if total_vol > 0 else None
        elapsed_s = max(0.0, (time.perf_counter_ns() - self._start_time_ns) / 1e9)

        return MarketState(
            orders_count=self.engine.get_order_count(),
            trades_count=len(trades),
            total_volume=total_vol,
            total_notional=total_notional,
            vwap=vwap,
            recent_trades=trades[-8:],
            recent_whale_events=list(self.whale_events)[-6:],
            last_order=self._last_order_info,
            engine_name=self.engine_name,
            is_paused=is_paused,
            rate_target=rate_target,
            elapsed_seconds=elapsed_s,
            persistence_stats=self.get_persistence_ui_stats(),
            active_whale_threshold=self.get_current_whale_threshold(),
        )

    def render_text_snapshot(self, depth: int = 5, trade_count: int = 6) -> str:
        """Generate a complete ASCII formatted snapshot of the dashboard."""
        book = self.get_top_of_book(depth=depth)
        lat = self.get_latency_stats()
        state = self.get_market_state()
        p_stat = state.persistence_stats

        lines = [
            "=" * 78,
            f" CHRONOSMATCH LATENCY DASHBOARD  |  Engine: {state.engine_name}",
            "=" * 78,
            f" Orders Ingested : {state.orders_count:<8}  Matched Trades : {state.trades_count:<8}  Throughput : {lat.throughput_ops:,.1f} ops/s",
            f" Total Volume    : {state.total_volume:<8}  VWAP           : ${(state.vwap or 0.0):<7.2f}  Elapsed    : {state.elapsed_seconds:0.1f}s",
            "-" * 78,
            f" PERSISTENCE AUDIT: [{p_stat.worker_state}] | Persisted: {p_stat.trades_persisted:,} | Queue: {p_stat.pending_queue_size} | DB Errors: {p_stat.failed_writes} | Write Lat: {p_stat.last_write_latency_ms:0.2f}ms",
            "-" * 78,
            f" {'TOP OF ORDER BOOK & SPREAD':<38} | {'LATENCY METRICS (per order)':<36}",
            "-" * 78,
        ]

        # Top of Book summary
        bid_str = f"${book.best_bid:.2f}" if book.best_bid is not None else "EMPTY"
        ask_str = f"${book.best_ask:.2f}" if book.best_ask is not None else "EMPTY"
        if book.spread is not None:
            spread_str = f"${book.spread:.2f} ({book.spread_bps:.1f} bps)"
        else:
            spread_str = "N/A (One-sided)"

        lines.append(f" Best Bid : {bid_str:<12} Best Ask : {ask_str:<10} |  Last Latency : {lat.last_us:8.2f} us")
        lines.append(f" Spread   : {spread_str:<25} |  Mean Latency : {lat.mean_us:8.2f} us")
        lines.append(f" Mid Price: ${(book.mid_price or 0.0):<10.2f}                  |  Median (p50) : {lat.p50_us:8.2f} us")
        lines.append(f" Book Status: {'CROSSED!' if book.is_crossed else 'NORMAL':<23}     |  p95 / p99    : {lat.p95_us:6.2f} / {lat.p99_us:6.2f} us")
        lines.append(f" Resting Orders: Bids={book.bid_order_count} Asks={book.ask_order_count:<12} |  Sparkline    : [{lat.sparkline}]")
        lines.append("-" * 78)

        # Depth ladder
        lines.append(f" {'ASKS (Sell Depth)':^37} | {'BIDS (Buy Depth)':^37}")
        lines.append(f"  {'Price':>10}  {'Qty':>10}  {'Depth':>10}    |   {'Price':>10}  {'Qty':>10}  {'Depth':>10}")
        lines.append(f"  {'-'*10}  {'-'*10}  {'-'*10}    |   {'-'*10}  {'-'*10}  {'-'*10}")

        max_rows = max(len(book.ask_levels), len(book.bid_levels), 1)
        ask_rev = list(reversed(book.ask_levels[:depth]))

        for i in range(max_rows):
            if i < len(ask_rev):
                ap, aq = ask_rev[i]
                ask_part = f"  ${ap:9.2f}  {aq:10d}  {'|'*min(10, max(1, aq//50)):<10}"
            else:
                ask_part = " " * 36

            if i < len(book.bid_levels):
                bp, bq = book.bid_levels[i]
                bid_part = f"  ${bp:9.2f}  {bq:10d}  {'|'*min(10, max(1, bq//50)):<10}"
            else:
                bid_part = " " * 36

            lines.append(f"{ask_part:<37} | {bid_part}")

        lines.append("-" * 78)
        # Whale Section
        lines.append(f" WHALE ORDERS & LARGE SWEEPS (Threshold >= {state.active_whale_threshold} Qty):")
        if not state.recent_whale_events:
            lines.append("  (No large orders/sweeps detected yet)")
        else:
            for w in state.recent_whale_events[-4:]:
                tag = "[WHALE TRADE]" if w.event_type == "MATCHED_TRADE" else "[WHALE ORDER]"
                lines.append(f"  {tag:<15} Side:{w.side:<4} Price:${w.price:7.2f} Qty:{w.quantity:<6} Notional:${w.notional:10,.2f}")

        lines.append("-" * 78)
        lines.append(f" RECENT MATCHED TRADES (Last {trade_count}):")
        lines.append(f"  {'Trade':>5}  {'Buy ID':>8}  {'Sell ID':>8}  {'Price':>10}  {'Quantity':>8}  {'Notional ($)':>12}  Flag")
        lines.append(f"  {'-'*5}  {'-'*8}  {'-'*8}  {'-'*10}  {'-'*8}  {'-'*12}  {'-'*5}")

        trades_to_show = state.recent_trades[-trade_count:]
        if not trades_to_show:
            lines.append("  (No trades executed yet — waiting for matching cross)")
        else:
            for idx, tr in enumerate(trades_to_show, 1):
                p = float(tr.get("price", 0.0))
                q = int(tr.get("qty", tr.get("quantity", 0)))
                flag = "<<< WHALE" if q >= state.active_whale_threshold else ""
                lines.append(
                    f"  {idx:5d}  {tr.get('buy_id', 0):8d}  {tr.get('sell_id', 0):8d}  "
                    f"${p:9.2f}  {q:8d}  ${(p * q):11.2f}  {flag}"
                )

        lines.append("=" * 78)
        return "\n".join(lines)

    def shutdown(self) -> None:
        """Cleanly flush and shut down worker thread and database connection."""
        if self.worker is not None:
            self.worker.stop(timeout=2.0)
            self.worker = None
        if self.repository is not None:
            self.repository.close()
            self.repository = None

    def __del__(self) -> None:
        try:
            self.shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Curses Terminal UI Dashboard
# ---------------------------------------------------------------------------

class CursesDashboard:
    """Raw curses terminal UI for real-time visualization of ChronosMatch."""

    COLOR_PAIR_BID = 1
    COLOR_PAIR_ASK = 2
    COLOR_PAIR_HEADER = 3
    COLOR_PAIR_SPREAD = 4
    COLOR_PAIR_WHALE = 5
    COLOR_PAIR_STATUS = 6

    def __init__(self, monitor: LatencyMonitor, target_rate: int = 30):
        self.monitor = monitor
        self.target_rate = max(1, target_rate)
        self.is_paused = False
        self.show_help = False
        self.running = True

    def color(self, pair_id: int) -> int:
        """Safely fetch curses color pair attribute or 0 if uninitialized."""
        try:
            return curses.color_pair(pair_id)
        except curses.error:
            return 0

    def init_colors(self) -> None:
        """Set up curses color palette if supported."""
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            try:
                curses.init_pair(self.COLOR_PAIR_BID, curses.COLOR_GREEN, -1)
                curses.init_pair(self.COLOR_PAIR_ASK, curses.COLOR_RED, -1)
                curses.init_pair(self.COLOR_PAIR_HEADER, curses.COLOR_CYAN, -1)
                curses.init_pair(self.COLOR_PAIR_SPREAD, curses.COLOR_YELLOW, -1)
                curses.init_pair(self.COLOR_PAIR_WHALE, curses.COLOR_MAGENTA, -1)
                curses.init_pair(self.COLOR_PAIR_STATUS, curses.COLOR_BLACK, curses.COLOR_CYAN)
            except curses.error:
                pass

    @staticmethod
    def safe_addstr(win: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
        """Safely write string to window boundary without throwing curses edge exceptions."""
        max_y, max_x = win.getmaxyx()
        if y < 0 or y >= max_y or x < 0 or x >= max_x:
            return
        clipped = text[: max_x - x - 1]
        try:
            win.addstr(y, x, clipped, attr)
        except curses.error:
            pass

    def draw_box(self, win: curses.window, y: int, x: int, h: int, w: int, title: str = "", attr: int = 0) -> None:
        """Draw a styled box border with optional title header."""
        max_y, max_x = win.getmaxyx()
        if y + h > max_y or x + w > max_x:
            return

        # Top border
        self.safe_addstr(win, y, x, "+" + "-" * (w - 2) + "+", attr)
        if title:
            t = f" {title} "
            if len(t) < w - 4:
                self.safe_addstr(win, y, x + 2, t, attr | curses.A_BOLD)

        # Side borders
        for row in range(y + 1, y + h - 1):
            self.safe_addstr(win, row, x, "|", attr)
            self.safe_addstr(win, row, x + w - 1, "|", attr)

        # Bottom border
        self.safe_addstr(win, y + h - 1, x, "+" + "-" * (w - 2) + "+", attr)

    def _render_compact(
        self,
        stdscr: curses.window,
        max_y: int,
        max_x: int,
        book: OrderBookTop,
        lat: LatencyStats,
        state: MarketState,
    ) -> None:
        """Render responsive compact dashboard for smaller terminals."""
        status = "PAUSED" if self.is_paused else "RUNNING"
        hdr = f"CHRONOSMATCH [{status}] | Rate:{self.target_rate}/s | Orders:{state.orders_count:,} | Trades:{state.trades_count:,}"
        self.safe_addstr(stdscr, 0, 0, hdr[:max_x - 1], self.color(self.COLOR_PAIR_HEADER) | curses.A_BOLD)

        bid_s = f"${book.best_bid:.2f}" if book.best_bid is not None else "EMPTY"
        ask_s = f"${book.best_ask:.2f}" if book.best_ask is not None else "EMPTY"
        spread_s = f"${book.spread:.2f} ({book.spread_bps:.1f}bps)" if book.spread is not None else "N/A"

        self.safe_addstr(stdscr, 1, 0, "BID: ", curses.A_BOLD)
        self.safe_addstr(stdscr, 1, 5, f"{bid_s:<8} ", self.color(self.COLOR_PAIR_BID) | curses.A_BOLD)
        self.safe_addstr(stdscr, 1, 14, "ASK: ", curses.A_BOLD)
        self.safe_addstr(stdscr, 1, 19, f"{ask_s:<8} ", self.color(self.COLOR_PAIR_ASK) | curses.A_BOLD)
        self.safe_addstr(stdscr, 1, 28, f"SPREAD: {spread_s}", self.color(self.COLOR_PAIR_SPREAD) | curses.A_BOLD)

        # Persistence indicator
        pst = state.persistence_stats
        p_txt = f"DB:[{pst.worker_state}] Persist:{pst.trades_persisted} Q:{pst.pending_queue_size} Lat:{pst.last_write_latency_ms:0.1f}ms"
        self.safe_addstr(stdscr, 2, 0, p_txt[:max_x - 1], curses.A_DIM)

        lat_txt = f"LATENCY: Mean:{lat.mean_us:5.1f}us | p50:{lat.p50_us:5.1f}us | p95:{lat.p95_us:5.1f}us | Ops:{lat.throughput_ops:,.0f}/s"
        self.safe_addstr(stdscr, 3, 0, lat_txt[:max_x - 1])

        if max_y > 4:
            self.safe_addstr(stdscr, 4, 0, "-" * min(max_x - 1, 75), curses.A_DIM)

        curr_y = 5
        avail_rows = max(0, max_y - curr_y - 1)
        recent = state.recent_trades[-avail_rows:] if avail_rows > 0 else []
        for idx, t in enumerate(recent):
            p = float(t.get("price", 0.0))
            q = int(t.get("qty", t.get("quantity", 0)))
            is_whale = q >= state.active_whale_threshold
            flag = "[WHALE]" if is_whale else ""
            t_line = f"TRD #{idx+1:03d} B:{t.get('buy_id',0)} S:{t.get('sell_id',0)} ${p:.2f} Q:{q:<4} {flag}"
            attr = self.color(self.COLOR_PAIR_WHALE) | curses.A_BOLD if is_whale else curses.A_NORMAL
            self.safe_addstr(stdscr, curr_y + idx, 0, t_line[:max_x - 1], attr)

        footer = "[q]Quit [p]Pause [s]Step [+/-]Rate [r]Reset [h]Help"
        self.safe_addstr(stdscr, max_y - 1, 0, footer[:max_x - 1], self.color(self.COLOR_PAIR_STATUS))
        stdscr.refresh()

    def render(self, stdscr: curses.window) -> None:
        """Render complete curses dashboard frame."""
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()

        # Minimum dimension guard
        if max_y < 8 or max_x < 45:
            msg1 = "ChronosMatch Latency Dashboard"
            msg2 = f"Window size ({max_x}x{max_y}) too small! Resize to at least 45x8."
            self.safe_addstr(stdscr, max_y // 2 - 1, max(0, (max_x - len(msg1)) // 2), msg1, curses.A_BOLD)
            self.safe_addstr(stdscr, max_y // 2, max(0, (max_x - len(msg2)) // 2), msg2, self.color(self.COLOR_PAIR_SPREAD))
            stdscr.refresh()
            return

        book = self.monitor.get_top_of_book(depth=5)
        lat = self.monitor.get_latency_stats()
        state = self.monitor.get_market_state(is_paused=self.is_paused, rate_target=self.target_rate)
        p_stat = state.persistence_stats

        # Compact layout check
        if max_y < 24 or max_x < 78:
            self._render_compact(stdscr, max_y, max_x, book, lat, state)
            return

        # -------------------------------------------------------------------
        # 1. Header Bar
        # -------------------------------------------------------------------
        header_title = " CHRONOSMATCH HFT DASHBOARD | Latency, Resilience & UI "
        engine_badge = f"[{state.engine_name}]"
        self.safe_addstr(stdscr, 0, 1, header_title, self.color(self.COLOR_PAIR_HEADER) | curses.A_BOLD)
        self.safe_addstr(stdscr, 0, max_x - len(engine_badge) - 2, engine_badge, self.color(self.COLOR_PAIR_STATUS))

        status_str = "PAUSED" if self.is_paused else "RUNNING"
        sub_hdr = (
            f" Status: {status_str} | Rate: {self.target_rate} ops/s | "
            f"Orders: {state.orders_count:,} | Trades: {state.trades_count:,} | "
            f"Elapsed: {state.elapsed_seconds:.1f}s"
        )
        self.safe_addstr(stdscr, 1, 1, sub_hdr, curses.A_DIM)

        # Split width for main panels
        left_w = max_x // 2
        right_w = max_x - left_w - 1
        panel_h = 11

        # -------------------------------------------------------------------
        # 2. Panel Left: Top of Order Book & Spread
        # -------------------------------------------------------------------
        self.draw_box(stdscr, 2, 0, panel_h, left_w, title="TOP OF ORDER BOOK & SPREAD", attr=self.color(self.COLOR_PAIR_HEADER))

        bid_str = f"${book.best_bid:7.2f}" if book.best_bid is not None else "  EMPTY "
        ask_str = f"${book.best_ask:7.2f}" if book.best_ask is not None else "  EMPTY "

        self.safe_addstr(stdscr, 3, 2, "BEST BID :", curses.A_BOLD)
        self.safe_addstr(stdscr, 3, 13, bid_str, self.color(self.COLOR_PAIR_BID) | curses.A_BOLD)

        self.safe_addstr(stdscr, 3, 23, "BEST ASK :", curses.A_BOLD)
        self.safe_addstr(stdscr, 3, 34, ask_str, self.color(self.COLOR_PAIR_ASK) | curses.A_BOLD)

        if book.spread is not None:
            spread_txt = f"${book.spread:5.2f} ({book.spread_bps:5.1f} bps)"
            color_id = self.COLOR_PAIR_SPREAD if not book.is_crossed else self.COLOR_PAIR_ASK
        else:
            spread_txt = "N/A (One-Sided)"
            color_id = self.COLOR_PAIR_SPREAD

        self.safe_addstr(stdscr, 4, 2, f"SPREAD   : {spread_txt}", self.color(color_id) | curses.A_BOLD)
        mid_txt = f"${book.mid_price:.2f}" if book.mid_price is not None else "N/A"
        self.safe_addstr(stdscr, 4, 25, f"MID PRICE: {mid_txt}", curses.A_DIM)

        self.safe_addstr(stdscr, 5, 2, "SIDE      PRICE     QTY   DEPTH", curses.A_UNDERLINE)

        curr_row = 6
        for p, q in reversed(book.ask_levels[:2]):
            bar = "#" * min(8, max(1, q // 60))
            line = f"ASK (S)  ${p:7.2f}  {q:6d}  {bar:<8}"
            self.safe_addstr(stdscr, curr_row, 2, line, self.color(self.COLOR_PAIR_ASK))
            curr_row += 1

        divider = f"-- SPREAD: {spread_txt} --"
        self.safe_addstr(stdscr, curr_row, 2, divider[:left_w - 4], self.color(self.COLOR_PAIR_SPREAD))
        curr_row += 1

        for p, q in book.bid_levels[:2]:
            bar = "#" * min(8, max(1, q // 60))
            line = f"BID (B)  ${p:7.2f}  {q:6d}  {bar:<8}"
            self.safe_addstr(stdscr, curr_row, 2, line, self.color(self.COLOR_PAIR_BID))
            curr_row += 1

        self.safe_addstr(
            stdscr,
            panel_h + 1,
            2,
            f"Resting Depth: Bids={book.total_bid_qty} | Asks={book.total_ask_qty}",
            curses.A_DIM,
        )

        # -------------------------------------------------------------------
        # 3. Panel Right: Latency & Persistence Performance
        # -------------------------------------------------------------------
        self.draw_box(stdscr, 2, left_w + 1, panel_h, right_w, title="LATENCY & PERSISTENCE", attr=self.color(self.COLOR_PAIR_HEADER))

        rx = left_w + 3
        self.safe_addstr(stdscr, 3, rx, f"Engine Latency: Last:{lat.last_us:5.1f}us | Mean:{lat.mean_us:5.1f}us", curses.A_BOLD)
        self.safe_addstr(stdscr, 4, rx, f"Median (p50)  : {lat.p50_us:5.1f}us  p95/p99: {lat.p95_us:5.1f}/{lat.p99_us:5.1f}us")
        self.safe_addstr(stdscr, 5, rx, f"Throughput    : {lat.throughput_ops:9.1f} orders/sec", self.color(self.COLOR_PAIR_BID) | curses.A_BOLD)
        self.safe_addstr(stdscr, 6, rx, f"Trend Sparkline: [{lat.sparkline}]", self.color(self.COLOR_PAIR_HEADER))

        # Persistence block
        self.safe_addstr(stdscr, 7, rx, "-" * (right_w - 6), curses.A_DIM)
        p_color = self.COLOR_PAIR_BID if p_stat.worker_state == "RUNNING" else self.COLOR_PAIR_SPREAD
        self.safe_addstr(stdscr, 8, rx, f"DB Worker     : [{p_stat.worker_state}]", self.color(p_color) | curses.A_BOLD)
        self.safe_addstr(stdscr, 8, rx + 24, f"Persisted: {p_stat.trades_persisted:,}")
        self.safe_addstr(stdscr, 9, rx, f"Pending Queue : {p_stat.pending_queue_size:<6} DB Errors: {p_stat.failed_writes}")
        self.safe_addstr(stdscr, 10, rx, f"DB Batch Write: {p_stat.last_write_latency_ms:0.2f}ms | Queue Lat: {p_stat.last_queue_latency_us:0.1f}us", curses.A_DIM)

        # -------------------------------------------------------------------
        # 4. Panel Middle: Whale Orders & Large Sweeps
        # -------------------------------------------------------------------
        whale_y = 2 + panel_h + 1
        whale_h = 5
        self.draw_box(
            stdscr,
            whale_y,
            0,
            whale_h,
            max_x - 1,
            title=f"WHALE ORDERS & LARGE SWEEPS (Threshold >= {state.active_whale_threshold} Qty)",
            attr=self.color(self.COLOR_PAIR_WHALE),
        )

        recent_whales = state.recent_whale_events[-(whale_h - 2):]
        if not recent_whales:
            self.safe_addstr(stdscr, whale_y + 1, 2, "Waiting for large liquidity events or multi-level sweeps...", curses.A_DIM)
        else:
            for idx, w in enumerate(recent_whales):
                w_tag = "[WHALE TRADE]" if w.event_type == "MATCHED_TRADE" else "[WHALE ORDER]"
                w_str = f" #{idx+1} {w_tag:<14} Side:{w.side:<4} Price:${w.price:7.2f} Qty:{w.quantity:<5} Notional:${w.notional:10,.2f}"
                self.safe_addstr(stdscr, whale_y + 1 + idx, 2, w_str, self.color(self.COLOR_PAIR_WHALE) | curses.A_BOLD)

        # -------------------------------------------------------------------
        # 5. Panel Bottom: Matched Trade Execution Tape
        # -------------------------------------------------------------------
        tape_y = whale_y + whale_h
        tape_h = max_y - tape_y - 2
        self.draw_box(stdscr, tape_y, 0, tape_h, max_x - 1, title="MATCHED TRADE TAPE (Price-Time Priority)", attr=self.color(self.COLOR_PAIR_HEADER))

        header_tape = " TRADE #   BUY ID    SELL ID     PRICE     QTY       NOTIONAL     FLAG"
        self.safe_addstr(stdscr, tape_y + 1, 2, header_tape, curses.A_UNDERLINE)

        visible_trades = state.recent_trades[-(tape_h - 3):]
        for idx, t in enumerate(visible_trades):
            row_y = tape_y + 2 + idx
            p = float(t.get("price", 0.0))
            q = int(t.get("qty", t.get("quantity", 0)))
            notional = p * q
            is_whale = q >= state.active_whale_threshold
            flag_str = "[WHALE]" if is_whale else ""
            line = f" #{idx+1:04d}   {t.get('buy_id', 0):8d}  {t.get('sell_id', 0):8d}   ${p:7.2f}   {q:5d}   ${notional:11.2f}   {flag_str}"
            attr = self.color(self.COLOR_PAIR_WHALE) | curses.A_BOLD if is_whale else curses.A_NORMAL
            self.safe_addstr(stdscr, row_y, 2, line, attr)

        # -------------------------------------------------------------------
        # 6. Command Footer / Help
        # -------------------------------------------------------------------
        footer_text = " [q] Quit  [p] Pause/Resume  [s] Single-step  [+] Faster  [-] Slower  [r] Reset  [h] Help "
        self.safe_addstr(stdscr, max_y - 1, 0, footer_text[:max_x - 1], self.color(self.COLOR_PAIR_STATUS))

        if self.show_help:
            self._render_help_modal(stdscr, max_y, max_x)

        stdscr.refresh()

    def _render_help_modal(self, stdscr: curses.window, max_y: int, max_x: int) -> None:
        """Render floating help dialog."""
        h, w = 12, 60
        y = (max_y - h) // 2
        x = (max_x - w) // 2
        self.draw_box(stdscr, y, x, h, w, title="KEYBOARD CONTROLS", attr=self.color(self.COLOR_PAIR_SPREAD) | curses.A_BOLD)

        help_lines = [
            ("q / Q / ESC", "Exit the dashboard cleanly"),
            ("p / P / Space", "Toggle market feed pause / resume"),
            ("s / S", "Step forward by 1 order (when paused)"),
            ("+ / =", "Increase order generation rate"),
            ("- / _", "Decrease order generation rate"),
            ("r / R", "Reset matching engine, book, and stats"),
            ("h / ?", "Toggle this help modal"),
        ]
        for i, (key, desc) in enumerate(help_lines):
            self.safe_addstr(stdscr, y + 2 + i, x + 3, f"{key:<14} : {desc}")

    def run(self, stdscr: curses.window, max_orders: int | None = None) -> None:
        """Main non-blocking interactive curses loop."""
        curses.curs_set(0)
        stdscr.nodelay(True)
        self.init_colors()

        last_step_time = time.perf_counter()

        while self.running:
            now = time.perf_counter()
            step_interval = 1.0 / self.target_rate

            if not self.is_paused:
                if now - last_step_time >= step_interval:
                    self.monitor.step(num_orders=1)
                    last_step_time = now

            if max_orders is not None and self.monitor.engine.get_order_count() >= max_orders:
                break

            self.render(stdscr)

            try:
                ch = stdscr.getch()
            except curses.error:
                ch = -1

            if ch != -1:
                if ch in (ord("q"), ord("Q"), 27):  # 'q' or ESC
                    self.running = False
                elif ch in (ord("p"), ord("P"), ord(" ")):
                    self.is_paused = not self.is_paused
                elif ch in (ord("s"), ord("S")):
                    self.monitor.step(num_orders=1)
                elif ch in (ord("+"), ord("=")):
                    self.target_rate = min(500, self.target_rate + 5)
                elif ch in (ord("-"), ord("_")):
                    self.target_rate = max(1, self.target_rate - 5)
                elif ch in (ord("r"), ord("R")):
                    self.monitor.reset()
                elif ch in (ord("h"), ord("H"), ord("?")):
                    self.show_help = not self.show_help

            curses.napms(15)


# ---------------------------------------------------------------------------
# Fallback Headless / Plain Terminal Runner
# ---------------------------------------------------------------------------

def run_headless_loop(
    monitor: LatencyMonitor,
    target_rate: int = 30,
    max_orders: int | None = None,
    refresh_interval: float = 0.5,
) -> None:
    """Stream formatted ASCII dashboard updates to stdout when curses is unavailable."""
    print("[latency_monitor] Running in terminal text mode (Press Ctrl+C to stop)...")
    last_draw = 0.0
    orders_processed = 0

    try:
        while True:
            monitor.step(num_orders=1)
            orders_processed += 1

            now = time.time()
            if now - last_draw >= refresh_interval:
                os.system("cls" if os.name == "nt" else "clear")
                print(monitor.render_text_snapshot())
                last_draw = now

            if max_orders is not None and orders_processed >= max_orders:
                break

            time.sleep(1.0 / max(1, target_rate))
    except KeyboardInterrupt:
        print("\n[latency_monitor] Stopped by user.")
    finally:
        monitor.shutdown()


# ---------------------------------------------------------------------------
# Main CLI Entrypoint
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for launching Member 3's latency dashboard."""
    parser = argparse.ArgumentParser(
        description="ChronosMatch Real-Time Latency Dashboard & Order Book Monitor"
    )
    parser.add_argument("--rate", type=int, default=30, help="Target order simulation rate (orders/sec, default: 30)")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic RNG seed (default: 42)")
    parser.add_argument("--base-price", type=float, default=100.0, help="Initial market base price (default: 100.0)")
    parser.add_argument("--whale-threshold", type=int, default=500, help="Quantity threshold for whale flag (default: 500)")
    parser.add_argument("--whale-mode", type=str, default="absolute", choices=["absolute", "relative"], help="Whale detection mode (default: absolute)")
    parser.add_argument("--max-orders", type=int, default=None, help="Stop after N orders (default: infinite)")
    parser.add_argument("--no-curses", action="store_true", help="Force ASCII terminal mode without curses")
    parser.add_argument("--once", action="store_true", help="Print a single snapshot and exit (useful for CI/testing)")
    parser.add_argument("--no-persistence", action="store_true", help="Disable background SQLite persistence worker")
    parser.add_argument("--db-path", type=str, default="chronosmatch_trades.db", help="Path to SQLite persistence ledger")

    args = parser.parse_args(argv)

    monitor = LatencyMonitor(
        seed=args.seed,
        base_price=args.base_price,
        whale_threshold=args.whale_threshold,
        whale_mode=args.whale_mode,
        enable_persistence=not args.no_persistence,
        db_path=args.db_path,
    )

    try:
        if args.once:
            monitor.step(num_orders=max(10, args.max_orders or 10))
            print(monitor.render_text_snapshot())
            return 0

        if args.no_curses or not sys.stdin.isatty():
            run_headless_loop(
                monitor=monitor,
                target_rate=args.rate,
                max_orders=args.max_orders,
            )
            return 0

        dashboard = CursesDashboard(monitor=monitor, target_rate=args.rate)
        try:
            curses.wrapper(dashboard.run, max_orders=args.max_orders)
        except Exception as exc:
            print(f"[latency_monitor] Curses error: {exc}. Falling back to plain text mode...")
            run_headless_loop(
                monitor=monitor,
                target_rate=args.rate,
                max_orders=args.max_orders,
            )
    finally:
        monitor.shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(main())
