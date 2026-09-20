"""Member 3 pytest suite: metrics, visualizer, simulator, benchmark + integration.

Run from repo root:
    pytest src/analysis/tests/test_analysis.py -v
"""

import os
import sys

# repo root on sys.path so `src.*` and member modules resolve
_HERE = os.path.dirname(os.path.abspath(__file__))          # .../analysis/tests
_ANALYSIS = os.path.dirname(_HERE)                          # .../analysis
_SRC = os.path.dirname(_ANALYSIS)                           # .../src
_ROOT = os.path.dirname(_SRC)                               # repo root
for _p in (_ROOT, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from src.analysis.benchmark import ipc_audit, run_benchmark
from src.analysis.metrics import LatencyTracker, OrderBookAnalytics, percentile
from src.analysis.simulator import MockMarketSimulator
from src.analysis.visualizer import (
    ascii_histogram,
    render_latency_text,
    render_order_book_text,
    render_trades_text,
)


def _toy_book():
    from src.model.baseline_matcher import Order, OrderBook
    book = OrderBook()
    book.add_order(Order(1, "buy", 101.0, 50, 1))
    book.add_order(Order(2, "sell", 100.0, 20, 2))
    return book


# ---------- metrics ----------

def test_percentile_basic():
    assert percentile([], 50) == 0.0
    assert percentile([5], 99) == 5.0
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4], 0) == 1.0
    assert percentile([1, 2, 3, 4], 100) == 4.0


def test_latency_tracker_stats():
    t = LatencyTracker()
    t.start(0)
    for v in [1000, 2000, 3000, 4000]:
        t.add(v)
    t.stop(4_000_000_000)  # 4 s wall -> 1 order/s
    s = t.stats()
    assert s["count"] == 4
    assert s["min_ns"] == 1000
    assert s["max_ns"] == 4000
    assert abs(s["mean_us"] - 2.5) < 1e-9
    assert s["throughput_ops"] == 1.0


def test_latency_tracker_empty():
    assert LatencyTracker().stats()["count"] == 0


def test_snapshot_and_spread():
    book = _toy_book()
    snap = OrderBookAnalytics.snapshot(book)
    # buy 101 x50 matched against sell 100 x20 -> buy rests with 30
    assert snap.best_bid == 101.0
    assert snap.best_ask is None or snap.ask_depth == 0
    assert snap.bid_qty == 30


def test_snapshot_empty_book():
    from src.model.baseline_matcher import OrderBook
    snap = OrderBookAnalytics.snapshot(OrderBook())
    assert snap.best_bid is None and snap.best_ask is None
    assert snap.spread is None


def test_vwap_and_fill_rate():
    trades = [{"buy_id": 1, "sell_id": 2, "price": 100.0, "qty": 10},
              {"buy_id": 1, "sell_id": 3, "price": 102.0, "qty": 10}]
    assert OrderBookAnalytics.vwap(trades) == 101.0
    assert OrderBookAnalytics.vwap([]) is None
    assert OrderBookAnalytics.fill_rate(trades, 40) == 0.5
    assert OrderBookAnalytics.fill_rate([], 0) == 0.0


def test_whale_detection():
    trades = [{"buy_id": 1, "sell_id": 2, "price": 100.0, "qty": 600},
              {"buy_id": 3, "sell_id": 4, "price": 100.0, "qty": 10}]
    whales = OrderBookAnalytics.whale_trades(trades, 500)
    assert len(whales) == 1 and whales[0]["qty"] == 600


def test_depth_by_price():
    from src.model.baseline_matcher import Order
    orders = [Order(1, "buy", 100.0, 10, 1), Order(2, "buy", 100.0, 5, 2)]
    assert OrderBookAnalytics.depth_by_price(orders) == {100.0: 15}


# ---------- visualizer ----------

def test_render_order_book_text():
    text = render_order_book_text(_toy_book())
    assert "CHRONOSMATCH" in text and "Best Bid" in text or "one-sided" in text


def test_render_trades_whale_flag():
    trades = [{"buy_id": 1, "sell_id": 2, "price": 100.0, "qty": 999}]
    text = render_trades_text(trades, whale_threshold=500)
    assert "WHALE" in text


def test_render_latency_text():
    t = LatencyTracker()
    t.add(1500)
    text = render_latency_text(t.stats())
    assert "p95" in text


def test_ascii_histogram():
    assert "(no data)" in ascii_histogram([])
    h = ascii_histogram([1, 2, 3, 4, 5])
    assert "#" in h


# ---------- simulator ----------

def test_simulator_deterministic():
    s1 = MockMarketSimulator(seed=123)
    s2 = MockMarketSimulator(seed=123)
    assert s1.generate(20) == s2.generate(20)


def test_simulator_valid_orders():
    orders = MockMarketSimulator(seed=1).generate(50)
    assert len(orders) == 50
    for o in orders:
        assert o["side"] in ("BUY", "SELL")
        assert o["price"] > 0 and o["quantity"] > 0


def test_balanced_book_guarantees_trades():
    from src.model.baseline_matcher import Order, OrderBook
    sim = MockMarketSimulator(seed=0)
    orders = sim.balanced_book(10)
    book = OrderBook()
    for o in orders:
        side = "buy" if o["side"] == "BUY" else "sell"
        book.add_order(Order(o["order_id"], side, o["price"], o["quantity"], o["order_id"]))
    assert len(book.trades) > 0


# ---------- integration with Member 1 + 2 ----------

def test_member1_roundtrip():
    from src.data_processing.processor import DataProcessor
    p = DataProcessor()
    packed = p.process_order(order_id=7, side="BUY", price=50.5, quantity=9)
    assert len(packed) == p.record_size()
    back = p.unpack_order(packed)
    assert (back.order_id, back.side, back.price, back.quantity) == (7, "BUY", 50.5, 9)


def test_member1_member2_adapter():
    from src.data_processing.processor import DataProcessor
    from src.model.baseline_matcher import OrderBook
    from src.analysis.benchmark import _to_member2_order
    p = DataProcessor()
    packed = p.process_order(order_id=1, side="BUY", price=100.0, quantity=10)
    m2 = _to_member2_order(p.unpack_order(packed))
    assert m2.side == "buy" and m2.qty == 10
    book = OrderBook()
    book.add_order(m2)
    assert len(book.buy_orders) == 1


def test_run_benchmark_small():
    result, book, tracker = run_benchmark(n_orders=200, seed=42)
    assert result.num_orders == 200
    assert result.orders_per_sec > 0
    assert result.latency_stats["count"] == 200
    assert result.latency_stats["mean_us"] > 0
    assert result.trade_stats["count"] >= 0
    assert 0.0 <= result.fill_rate <= 1.0


def test_ipc_audit_small():
    out = ipc_audit(num_orders=500, seed=7)
    assert out["num_orders"] == 500
    assert out["orders_per_sec"] > 0
    assert out["record_size"] == 33
    assert out["total_bytes"] == 500 * 33


def test_price_time_priority_preserved():
    from src.model.baseline_matcher import Order, OrderBook
    book = OrderBook()
    book.add_order(Order(1, "sell", 100.0, 10, 2))
    book.add_order(Order(2, "sell", 100.0, 10, 1))  # earlier ts, same price
    book.add_order(Order(3, "buy", 100.0, 10, 3))
    assert book.trades[0]["sell_id"] == 2  # earliest timestamp matched first
