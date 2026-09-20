"""End-to-end benchmark harness for ChronosMatch (Member 3).

Pipeline per order:
  dict -> Member1 DataProcessor.pack_order (binary) -> unpack ->
  Member2 OrderBook.add_order, timed with time.perf_counter_ns().

Also provides ipc_audit(): pure pack/unpack throughput without matching.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field

# Make Member 1 / Member 2 importable whether tests run from repo root,
# from src/, or from src/analysis/.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)          # src/
_ROOT = os.path.dirname(_SRC)          # repo root
for _p in (_ROOT, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:  # Member 1
    from data_processing.processor import DataProcessor  # type: ignore
except Exception:  # pragma: no cover
    from src.data_processing.processor import DataProcessor  # type: ignore

try:  # Member 2
    from model.baseline_matcher import OrderBook as Member2OrderBook  # type: ignore
    from model.baseline_matcher import Order as Member2Order  # type: ignore
    try:
        from model.baseline_matcher import adapt_order as _official_adapt  # type: ignore
    except Exception:
        _official_adapt = None  # type: ignore
except Exception:  # pragma: no cover
    from src.model.baseline_matcher import OrderBook as Member2OrderBook  # type: ignore
    from src.model.baseline_matcher import Order as Member2Order  # type: ignore
    try:
        from src.model.baseline_matcher import adapt_order as _official_adapt  # type: ignore
    except Exception:
        _official_adapt = None  # type: ignore

from .metrics import LatencyTracker, OrderBookAnalytics
from .simulator import MockMarketSimulator


@dataclass
class BenchmarkResult:
    num_orders: int = 0
    elapsed_s: float = 0.0
    orders_per_sec: float = 0.0
    latency_stats: dict = field(default_factory=dict)
    trade_stats: dict = field(default_factory=dict)
    snapshot: object = None
    fill_rate: float = 0.0
    whale_count: int = 0
    latencies_ns: list[int] = field(default_factory=list)


def _to_member2_order(unpacked) -> Member2Order:
    """Adapt Member 1 unpacked order -> Member 2 Order.

    Prefers Member 2's official adapt_order() (added in PR #4) so the
    bridge always matches Member 2's documented schema; falls back to
    the equivalent local conversion on older checkouts.
    """
    if _official_adapt is not None:
        return _official_adapt(unpacked)
    side = str(getattr(unpacked, "side", "BUY")).lower()  # 'buy'/'sell'
    qty = int(getattr(unpacked, "quantity", getattr(unpacked, "qty", 0)))
    ts = int(getattr(unpacked, "timestamp_ns", getattr(unpacked, "timestamp", 0)))
    return Member2Order(
        int(getattr(unpacked, "order_id")),
        side,
        float(getattr(unpacked, "price")),
        qty,
        ts,
    )


def run_benchmark(n_orders: int = 5000, seed: int = 42,
                  whale_prob: float = 0.02,
                  whale_threshold: int = 500) -> tuple[BenchmarkResult, object, LatencyTracker]:
    sim = MockMarketSimulator(seed=seed)
    orders = sim.generate(n_orders, whale_prob=whale_prob)

    processor = DataProcessor()
    book = Member2OrderBook()
    tracker = LatencyTracker()

    submitted_qty = sum(int(o["quantity"]) for o in orders)

    t0 = time.perf_counter_ns()
    tracker.start(t0)
    latencies: list[int] = []
    for o in orders:
        enter = time.perf_counter_ns()
        packed = processor.process_order(
            order_id=int(o["order_id"]), side=str(o["side"]),
            price=float(o["price"]), quantity=int(o["quantity"]))
        unpacked = processor.unpack_order(packed)
        m2 = _to_member2_order(unpacked)
        book.add_order(m2)
        exit_ns = time.perf_counter_ns()
        lat = exit_ns - enter
        latencies.append(lat)
        tracker.add(lat)
    t1 = time.perf_counter_ns()
    tracker.stop(t1)

    elapsed_s = (t1 - t0) / 1e9
    stats = tracker.stats()
    trades = list(getattr(book, "trades", []))
    result = BenchmarkResult(
        num_orders=n_orders,
        elapsed_s=elapsed_s,
        orders_per_sec=(n_orders / elapsed_s) if elapsed_s > 0 else 0.0,
        latency_stats=stats,
        trade_stats=OrderBookAnalytics.trade_stats(trades),
        snapshot=OrderBookAnalytics.snapshot(book),
        fill_rate=OrderBookAnalytics.fill_rate(trades, submitted_qty),
        whale_count=len(OrderBookAnalytics.whale_trades(trades, whale_threshold)),
        latencies_ns=latencies,
    )
    return result, book, tracker


def ipc_audit(num_orders: int = 20000, seed: int = 7) -> dict:
    """Pure serialization round-trip (Member 1) — no matching.

    Proves the zero-copy-style binary path avoids pickle overhead and
    reports pack/unpack throughput.
    """
    sim = MockMarketSimulator(seed=seed)
    orders = sim.generate(num_orders)
    processor = DataProcessor()

    t0 = time.perf_counter_ns()
    total_bytes = 0
    for o in orders:
        packed = processor.process_order(
            order_id=int(o["order_id"]), side=str(o["side"]),
            price=float(o["price"]), quantity=int(o["quantity"]))
        total_bytes += len(packed)
        unpacked = processor.unpack_order(packed)
        assert unpacked.order_id == o["order_id"]
    t1 = time.perf_counter_ns()
    elapsed_s = (t1 - t0) / 1e9
    return {
        "num_orders": num_orders,
        "elapsed_s": elapsed_s,
        "orders_per_sec": (num_orders / elapsed_s) if elapsed_s > 0 else 0.0,
        "total_bytes": total_bytes,
        "mb_per_sec": (total_bytes / 1e6 / elapsed_s) if elapsed_s > 0 else 0.0,
        "record_size": processor.record_size(),
    }


def save_report(result: BenchmarkResult, book, out_dir: str = "reports",
                prefix: str = "chronosmatch") -> dict[str, str | None]:
    """Save text dashboard + PNG plots. Returns paths created."""
    from .visualizer import (plot_latency_histogram, plot_orderbook_depth,
                             plot_trade_prices, render_dashboard)
    os.makedirs(out_dir, exist_ok=True)
    lat_us = [v / 1000.0 for v in result.latencies_ns]
    trades = list(getattr(book, "trades", []))
    paths: dict[str, str | None] = {}
    txt_path = os.path.join(out_dir, f"{prefix}_dashboard.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(render_dashboard(book, trades, result.latency_stats))
        f.write(f"\n\norders/sec : {result.orders_per_sec:.1f}\n")
        f.write(f"fill_rate  : {result.fill_rate:.3f}\n")
        f.write(f"whales     : {result.whale_count}\n")
    paths["dashboard_txt"] = txt_path
    paths["latency_png"] = plot_latency_histogram(
        lat_us, os.path.join(out_dir, f"{prefix}_latency.png"))
    paths["depth_png"] = plot_orderbook_depth(
        book, os.path.join(out_dir, f"{prefix}_depth.png"))
    paths["trades_png"] = plot_trade_prices(
        trades, os.path.join(out_dir, f"{prefix}_trades.png"))
    return paths
