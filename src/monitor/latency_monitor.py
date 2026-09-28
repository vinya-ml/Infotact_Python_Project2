"""ChronosMatch Latency Dashboard & Order Book Monitor (Member 3).

Week 2 Responsibility:
Build the raw curses terminal UI to display the Top of Order Book,
Bid/Ask spread, and real-time market information.

Integrates with:
- Member 1's Cython engine foundation / DataProcessor
- Member 2's Cython Price-Time Priority matching engine (MatchingEngine)
- Member 3's Week 1 simulator and latency analytics
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

# Side constants matching SCHEMA.md & matching_engine.pyx
BUY = 0
SELL = 1


# ---------------------------------------------------------------------------
# MatchingEngine Import / High-Performance Fallback
# ---------------------------------------------------------------------------

try:
    from src.engine.matching_engine import MatchingEngine, BUY as ENGINE_BUY, SELL as ENGINE_SELL  # type: ignore
    HAS_CYTHON_ENGINE = True
except (ImportError, ModuleNotFoundError):
    # Pure-Python fallback matching the exact SCHEMA.md & matching_engine.pyx contract
    # so the dashboard runs out-of-the-box on platforms without C++ build tools.
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
    engine_name = "Cython (Native C)" if HAS_CYTHON_ENGINE else "Python Engine (Compiled .pyx Pending)"
    return engine, engine_name


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

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
class MarketState:
    """Aggregated market overview for real-time reporting."""
    orders_count: int = 0
    trades_count: int = 0
    total_volume: int = 0
    total_notional: float = 0.0
    vwap: float | None = None
    recent_trades: list[dict[str, Any]] = field(default_factory=list)
    last_order: dict[str, Any] | None = None
    engine_name: str = "Unknown"
    is_paused: bool = False
    rate_target: int = 30
    elapsed_seconds: float = 0.0


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
    """Coordinates matching engine execution, feeds, and real-time metrics."""

    def __init__(
        self,
        engine: Any | None = None,
        seed: int = 42,
        base_price: float = 100.0,
        whale_threshold: int = 500,
        rolling_window: int = 100,
    ):
        if engine is not None:
            self.engine = engine
            self.engine_name = type(engine).__name__
        else:
            self.engine, self.engine_name = get_matching_engine()

        self.seed = seed
        self.base_price = float(base_price)
        self.whale_threshold = int(whale_threshold)
        self.rolling_window = int(rolling_window)

        # Simulator feed
        from src.analysis.simulator import MockMarketSimulator
        self.simulator = MockMarketSimulator(seed=self.seed, base_price=self.base_price)

        # Latency collection
        self.all_latencies_ns: list[int] = []
        self.rolling_latencies_ns: collections.deque[int] = collections.deque(maxlen=self.rolling_window)
        self._start_time_ns: int = time.perf_counter_ns()
        self._last_order_info: dict[str, Any] | None = None
        self._last_latency_ns: int = 0

    def reset(self, new_seed: int | None = None) -> None:
        """Reset the monitor, clearing engine state, orders, and collected latencies."""
        self.engine, self.engine_name = get_matching_engine()
        if new_seed is not None:
            self.seed = new_seed
        self.simulator.reset(seed=self.seed)
        self.all_latencies_ns.clear()
        self.rolling_latencies_ns.clear()
        self._start_time_ns = time.perf_counter_ns()
        self._last_order_info = None
        self._last_latency_ns = 0

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

            # Measure order dispatch latency to matching engine in nanoseconds
            t0 = time.perf_counter_ns()
            self.engine.add_order(order_id, side_val, price_val, qty_val, ts_val)
            t1 = time.perf_counter_ns()

            lat = max(1, t1 - t0)
            self._last_latency_ns = lat
            self.all_latencies_ns.append(lat)
            self.rolling_latencies_ns.append(lat)
            latencies.append(lat)

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

        t0 = time.perf_counter_ns()
        res = self.engine.add_order(order_id, side_val, float(price), int(quantity), timestamp_ns)
        t1 = time.perf_counter_ns()

        lat = max(1, t1 - t0)
        self._last_latency_ns = lat
        self.all_latencies_ns.append(lat)
        self.rolling_latencies_ns.append(lat)
        return res

    def get_top_of_book(self, depth: int = 5) -> OrderBookTop:
        """Query top-of-book and aggregated depth ladder from the matching engine."""
        # Query snapshot using Member 2's get_order_book_snapshot()
        raw_snapshot = self.engine.get_order_book_snapshot()
        raw_bid, raw_ask = (raw_snapshot[0], raw_snapshot[1]) if raw_snapshot else (None, None)

        buys = list(getattr(self.engine, "buy_orders", []))
        sells = list(getattr(self.engine, "sell_orders", []))

        # Aggregate resting quantities by price
        bid_depth_map: dict[float, int] = collections.defaultdict(int)
        for b in buys:
            qty = getattr(b, "quantity", getattr(b, "qty", 0))
            bid_depth_map[float(b.price)] += int(qty)

        ask_depth_map: dict[float, int] = collections.defaultdict(int)
        for s in sells:
            qty = getattr(s, "quantity", getattr(s, "qty", 0))
            ask_depth_map[float(s.price)] += int(qty)

        # Sorted depth levels: Bids highest-to-lowest, Asks lowest-to-highest
        sorted_bids = sorted(bid_depth_map.items(), key=lambda x: -x[0])
        sorted_asks = sorted(ask_depth_map.items(), key=lambda x: x[0])

        best_bid = raw_bid if raw_bid is not None else (sorted_bids[0][0] if sorted_bids else None)
        best_ask = raw_ask if raw_ask is not None else (sorted_asks[0][0] if sorted_asks else None)

        spread: float | None = None
        spread_bps: float | None = None
        mid_price: float | None = None
        is_crossed = False

        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid
            mid_price = (best_ask + best_bid) / 2.0
            if mid_price > 0:
                spread_bps = (spread / mid_price) * 10000.0
            if best_bid >= best_ask:
                is_crossed = True

        return OrderBookTop(
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            spread_bps=spread_bps,
            mid_price=mid_price,
            bid_levels=sorted_bids[:depth],
            ask_levels=sorted_asks[:depth],
            total_bid_qty=sum(bid_depth_map.values()),
            total_ask_qty=sum(ask_depth_map.values()),
            bid_order_count=len(buys),
            ask_order_count=len(sells),
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

    def get_market_state(self, is_paused: bool = False, rate_target: int = 30) -> MarketState:
        """Compile comprehensive market summary."""
        trades = self.engine.get_trades()
        total_vol = sum(int(t.get("qty", 0)) for t in trades)
        total_notional = sum(float(t.get("price", 0.0)) * int(t.get("qty", 0)) for t in trades)
        vwap = (total_notional / total_vol) if total_vol > 0 else None
        elapsed_s = max(0.0, (time.perf_counter_ns() - self._start_time_ns) / 1e9)

        return MarketState(
            orders_count=self.engine.get_order_count(),
            trades_count=len(trades),
            total_volume=total_vol,
            total_notional=total_notional,
            vwap=vwap,
            recent_trades=trades[-8:],
            last_order=self._last_order_info,
            engine_name=self.engine_name,
            is_paused=is_paused,
            rate_target=rate_target,
            elapsed_seconds=elapsed_s,
        )

    def render_text_snapshot(self, depth: int = 5, trade_count: int = 6) -> str:
        """Generate a complete ASCII formatted snapshot of the dashboard."""
        book = self.get_top_of_book(depth=depth)
        lat = self.get_latency_stats()
        state = self.get_market_state()

        lines = [
            "=" * 78,
            f" CHRONOSMATCH LATENCY DASHBOARD (Member 3)  |  Engine: {state.engine_name}",
            "=" * 78,
            f" Orders Ingested : {state.orders_count:<8}  Matched Trades : {state.trades_count:<8}  Throughput : {lat.throughput_ops:,.1f} ops/s",
            f" Total Volume    : {state.total_volume:<8}  VWAP           : ${(state.vwap or 0.0):<7.2f}  Elapsed    : {state.elapsed_seconds:0.1f}s",
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
            # Ask side
            if i < len(ask_rev):
                ap, aq = ask_rev[i]
                ask_part = f"  ${ap:9.2f}  {aq:10d}  {'|'*min(10, max(1, aq//50)):<10}"
            else:
                ask_part = " " * 36

            # Bid side
            if i < len(book.bid_levels):
                bp, bq = book.bid_levels[i]
                bid_part = f"  ${bp:9.2f}  {bq:10d}  {'|'*min(10, max(1, bq//50)):<10}"
            else:
                bid_part = " " * 36

            lines.append(f"{ask_part:<37} | {bid_part}")

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
                q = int(tr.get("qty", 0))
                flag = "<<< WHALE" if q >= self.whale_threshold else ""
                lines.append(
                    f"  {idx:5d}  {tr.get('buy_id', 0):8d}  {tr.get('sell_id', 0):8d}  "
                    f"${p:9.2f}  {q:8d}  ${(p * q):11.2f}  {flag}"
                )

        lines.append("=" * 78)
        return "\n".join(lines)


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
                # Terminal colors fallback
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
        """Render responsive compact dashboard for smaller terminals (e.g. 50x9 to 75x21)."""
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

        lat_txt = f"LATENCY: Last:{lat.last_us:5.1f}us | Mean:{lat.mean_us:5.1f}us | p50:{lat.p50_us:5.1f}us | p95:{lat.p95_us:5.1f}us"
        self.safe_addstr(stdscr, 2, 0, lat_txt[:max_x - 1])

        vwap_s = f"${state.vwap:.2f}" if state.vwap is not None else "N/A"
        spark_txt = f"TREND: [{lat.sparkline}] | Ops/s: {lat.throughput_ops:,.0f} | VWAP: {vwap_s}"
        self.safe_addstr(stdscr, 3, 0, spark_txt[:max_x - 1], self.color(self.COLOR_PAIR_HEADER))

        if max_y > 5:
            self.safe_addstr(stdscr, 4, 0, "-" * min(max_x - 1, 75), curses.A_DIM)

        curr_y = 5
        avail_rows = max(0, max_y - curr_y - 1)
        recent = state.recent_trades[-avail_rows:] if avail_rows > 0 else []
        for idx, t in enumerate(recent):
            p = float(t.get("price", 0.0))
            q = int(t.get("qty", 0))
            is_whale = q >= self.monitor.whale_threshold
            flag = "[W]" if is_whale else ""
            t_line = f"TRD #{idx+1:03d} B:{t.get('buy_id',0)} S:{t.get('sell_id',0)} ${p:.2f} Q:{q:<4} {flag}"
            attr = self.color(self.COLOR_PAIR_WHALE) | curses.A_BOLD if is_whale else curses.A_NORMAL
            self.safe_addstr(stdscr, curr_y + idx, 0, t_line[:max_x - 1], attr)

        footer = "[q]Quit [p]Pause [s]Step [+/-]Rate (Tip: Expand terminal for full UI)"
        self.safe_addstr(stdscr, max_y - 1, 0, footer[:max_x - 1], self.color(self.COLOR_PAIR_STATUS))
        stdscr.refresh()

    def render(self, stdscr: curses.window) -> None:
        """Render complete curses dashboard frame."""
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()

        # Minimum dimension guard for tiny screens
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

        # Responsive layout: If window is compact (< 22 rows or < 76 cols), render compact mode
        if max_y < 22 or max_x < 76:
            self._render_compact(stdscr, max_y, max_x, book, lat, state)
            return

        # -------------------------------------------------------------------
        # Full Layout: 1. Header Bar
        # -------------------------------------------------------------------
        # -------------------------------------------------------------------
        header_title = " CHRONOSMATCH HFT DASHBOARD | Member 3: Latency & Order Book Monitor "
        engine_badge = f"[{state.engine_name}]"
        self.safe_addstr(stdscr, 0, 1, header_title, self.color(self.COLOR_PAIR_HEADER) | curses.A_BOLD)
        self.safe_addstr(stdscr, 0, max_x - len(engine_badge) - 2, engine_badge, self.color(self.COLOR_PAIR_STATUS))

        status_str = "PAUSED" if self.is_paused else "RUNNING"
        sub_hdr = (
            f" Status: {status_str} | Target Rate: {self.target_rate} ops/s | "
            f"Orders: {state.orders_count:,} | Trades: {state.trades_count:,} | "
            f"Elapsed: {state.elapsed_seconds:.1f}s"
        )
        self.safe_addstr(stdscr, 1, 1, sub_hdr, curses.A_DIM)

        # Split width for two main panels
        left_w = max_x // 2
        right_w = max_x - left_w - 1
        panel_h = 12

        # -------------------------------------------------------------------
        # 2. Panel Left: Top of Order Book & Spread
        # -------------------------------------------------------------------
        self.draw_box(stdscr, 2, 0, panel_h, left_w, title="TOP OF ORDER BOOK & SPREAD", attr=self.color(self.COLOR_PAIR_HEADER))

        # Best Bid / Ask / Spread indicators
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

        # Depth table headers
        self.safe_addstr(stdscr, 5, 2, "SIDE      PRICE     QTY   DEPTH", curses.A_UNDERLINE)

        # Asks Depth (Red) - show best 2 asks
        curr_row = 6
        for p, q in reversed(book.ask_levels[:2]):
            bar = "#" * min(8, max(1, q // 60))
            line = f"ASK (S)  ${p:7.2f}  {q:6d}  {bar:<8}"
            self.safe_addstr(stdscr, curr_row, 2, line, self.color(self.COLOR_PAIR_ASK))
            curr_row += 1

        # Spread divider line
        divider = f"-- SPREAD: {spread_txt} --"
        self.safe_addstr(stdscr, curr_row, 2, divider[:left_w - 4], self.color(self.COLOR_PAIR_SPREAD))
        curr_row += 1

        # Bids Depth (Green) - show best 2 bids
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
        # 3. Panel Right: Latency & Throughput Metrics
        # -------------------------------------------------------------------
        self.draw_box(stdscr, 2, left_w + 1, panel_h, right_w, title="LATENCY & PERFORMANCE", attr=self.color(self.COLOR_PAIR_HEADER))

        rx = left_w + 3
        self.safe_addstr(stdscr, 3, rx, f"Last Latency : {lat.last_us:7.2f} us   ({lat.last_ns} ns)", curses.A_BOLD)
        self.safe_addstr(stdscr, 4, rx, f"Mean Latency : {lat.mean_us:7.2f} us   Min : {lat.min_us:6.2f} us")
        self.safe_addstr(stdscr, 5, rx, f"Median (p50) : {lat.p50_us:7.2f} us   Max : {lat.max_us:6.2f} us")
        self.safe_addstr(stdscr, 6, rx, f"p95 / p99    : {lat.p95_us:6.2f} us / {lat.p99_us:6.2f} us")

        # Sparkline
        self.safe_addstr(stdscr, 7, rx, "Latency Trend: [", curses.A_BOLD)
        self.safe_addstr(stdscr, 7, rx + 16, lat.sparkline, self.color(self.COLOR_PAIR_HEADER))
        self.safe_addstr(stdscr, 7, rx + 16 + len(lat.sparkline), "]")

        self.safe_addstr(stdscr, 8, rx, f"Throughput   : {lat.throughput_ops:9.1f} orders/sec", self.color(self.COLOR_PAIR_BID) | curses.A_BOLD)
        vwap_str = f"${state.vwap:.2f}" if state.vwap is not None else "N/A"
        self.safe_addstr(stdscr, 9, rx, f"Traded Volume: {state.total_volume:<7} VWAP: {vwap_str}")
        self.safe_addstr(stdscr, 10, rx, f"Total Value  : ${state.total_notional:,.2f}")

        # -------------------------------------------------------------------
        # 4. Panel Bottom: Matched Trade Execution Tape
        # -------------------------------------------------------------------
        tape_y = 2 + panel_h
        tape_h = max_y - tape_y - 2
        self.draw_box(stdscr, tape_y, 0, tape_h, max_x - 1, title="MATCHED TRADE TAPE (Price-Time Priority)", attr=self.color(self.COLOR_PAIR_HEADER))

        header_tape = " TRADE #   BUY ID    SELL ID     PRICE     QTY       NOTIONAL     FLAG"
        self.safe_addstr(stdscr, tape_y + 1, 2, header_tape, curses.A_UNDERLINE)

        visible_trades = state.recent_trades[-(tape_h - 3):]
        for idx, t in enumerate(visible_trades):
            row_y = tape_y + 2 + idx
            p = float(t.get("price", 0.0))
            q = int(t.get("qty", 0))
            notional = p * q
            is_whale = q >= self.monitor.whale_threshold
            flag_str = "[WHALE]" if is_whale else ""
            line = f" #{idx+1:04d}   {t.get('buy_id', 0):8d}  {t.get('sell_id', 0):8d}   ${p:7.2f}   {q:5d}   ${notional:11.2f}   {flag_str}"
            attr = self.color(self.COLOR_PAIR_WHALE) | curses.A_BOLD if is_whale else curses.A_NORMAL
            self.safe_addstr(stdscr, row_y, 2, line, attr)

        # -------------------------------------------------------------------
        # 5. Help Overlay / Command Footer
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

            # Process simulation steps
            if not self.is_paused:
                if now - last_step_time >= step_interval:
                    self.monitor.step(num_orders=1)
                    last_step_time = now

            if max_orders is not None and self.monitor.engine.get_order_count() >= max_orders:
                break

            # Render UI
            self.render(stdscr)

            # Keyboard handler
            try:
                ch = stdscr.getch()
            except curses.error:
                ch = -1

            if ch != -1:
                if ch in (ord("q"), ord("Q"), 27):  # 'q' or ESC
                    self.running = False
                elif ch in (ord("p"), ord("P"), ord(" ")):  # pause/resume
                    self.is_paused = not self.is_paused
                elif ch in (ord("s"), ord("S")):  # single step
                    self.monitor.step(num_orders=1)
                elif ch in (ord("+"), ord("=")):  # increase rate
                    self.target_rate = min(500, self.target_rate + 5)
                elif ch in (ord("-"), ord("_")):  # decrease rate
                    self.target_rate = max(1, self.target_rate - 5)
                elif ch in (ord("r"), ord("R")):  # reset
                    self.monitor.reset()
                elif ch in (ord("h"), ord("H"), ord("?")):  # toggle help
                    self.show_help = not self.show_help

            # Sleep briefly to avoid 100% CPU spinning
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
                # Clear terminal screen portably
                os.system("cls" if os.name == "nt" else "clear")
                print(monitor.render_text_snapshot())
                last_draw = now

            if max_orders is not None and orders_processed >= max_orders:
                break

            time.sleep(1.0 / max(1, target_rate))
    except KeyboardInterrupt:
        print("\n[latency_monitor] Stopped by user.")


# ---------------------------------------------------------------------------
# Main CLI Entrypoint
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for launching Member 3's latency dashboard."""
    parser = argparse.ArgumentParser(
        description="ChronosMatch Real-Time Latency Dashboard & Order Book Monitor (Member 3)"
    )
    parser.add_argument("--rate", type=int, default=30, help="Target order simulation rate (orders/sec, default: 30)")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic RNG seed (default: 42)")
    parser.add_argument("--base-price", type=float, default=100.0, help="Initial market base price (default: 100.0)")
    parser.add_argument("--whale-threshold", type=int, default=500, help="Quantity threshold for whale flag (default: 500)")
    parser.add_argument("--max-orders", type=int, default=None, help="Stop after N orders (default: infinite)")
    parser.add_argument("--no-curses", action="store_true", help="Force ASCII terminal mode without curses")
    parser.add_argument("--once", action="store_true", help="Print a single snapshot and exit (useful for CI/testing)")

    args = parser.parse_args(argv)

    monitor = LatencyMonitor(
        seed=args.seed,
        base_price=args.base_price,
        whale_threshold=args.whale_threshold,
    )

    # Single snapshot execution mode
    if args.once:
        monitor.step(num_orders=max(10, args.max_orders or 10))
        print(monitor.render_text_snapshot())
        return 0

    # Explicit or forced non-curses mode
    if args.no_curses or not sys.stdin.isatty():
        run_headless_loop(
            monitor=monitor,
            target_rate=args.rate,
            max_orders=args.max_orders,
        )
        return 0

    # Raw curses terminal dashboard
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
