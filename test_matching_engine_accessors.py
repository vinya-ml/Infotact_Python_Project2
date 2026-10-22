"""Tests for the engine's read accessors: get_trades_since, get_book_depth,
and the Python-visible constants. Requires the compiled engine.

Run from the project root:
    pytest -q test_matching_engine_accessors.py
"""

import collections
import random

import pytest

from src.engine import HAS_CYTHON_ENGINE
from src.model.baseline_matcher import Order, OrderBook

pytestmark = pytest.mark.skipif(
    not HAS_CYTHON_ENGINE,
    reason="compiled engine not built (python setup.py build_ext --inplace)")

if HAS_CYTHON_ENGINE:
    from src.engine.matching_engine import MatchingEngine, BUY, SELL


def test_constants_are_python_visible():
    from src.engine import matching_engine as mod
    assert (mod.BUY, mod.SELL, mod.HAS_CYTHON_ENGINE) == (0, 1, True)


def test_get_trades_since_returns_only_newer_trades():
    eng = MatchingEngine()
    eng.add_order(1, BUY, 10.0, 5, 1)
    eng.add_order(2, SELL, 10.0, 3, 2)
    eng.add_order(3, SELL, 10.0, 2, 3)
    everything = eng.get_trades()
    assert len(everything) == 2
    assert eng.get_trades_since(0) == everything
    assert eng.get_trades_since(1) == everything[1:]
    assert eng.get_trades_since(2) == []
    assert eng.get_trades_since(99) == []


def test_get_book_depth_aggregates_levels_best_first():
    eng = MatchingEngine()
    eng.add_order(1, BUY, 99.0, 10, 1)
    eng.add_order(2, BUY, 99.0, 5, 2)      # same level as order 1
    eng.add_order(3, BUY, 98.0, 7, 3)
    eng.add_order(4, SELL, 101.0, 4, 4)
    eng.add_order(5, SELL, 102.0, 6, 5)
    d = eng.get_book_depth(5)
    assert d["bid_levels"] == [(99.0, 15), (98.0, 7)]
    assert d["ask_levels"] == [(101.0, 4), (102.0, 6)]
    assert (d["total_bid_qty"], d["total_ask_qty"]) == (22, 10)
    assert (d["bid_order_count"], d["ask_order_count"]) == (3, 2)


def test_get_book_depth_limits_levels_but_not_totals():
    eng = MatchingEngine()
    for i, p in enumerate([99.0, 98.0, 97.0, 96.0]):
        eng.add_order(i, BUY, p, 10, i + 1)
    d = eng.get_book_depth(2)
    assert d["bid_levels"] == [(99.0, 10), (98.0, 10)]
    assert d["total_bid_qty"] == 40            # all four levels still counted
    assert d["bid_order_count"] == 4
    assert eng.get_book_depth(0)["bid_levels"] == []


def test_get_book_depth_empty_book():
    d = MatchingEngine().get_book_depth(5)
    assert d["bid_levels"] == [] and d["ask_levels"] == []
    assert d["total_bid_qty"] == d["total_ask_qty"] == 0
    assert d["bid_order_count"] == d["ask_order_count"] == 0


def _levels(orders, reverse):
    agg = collections.defaultdict(int)
    for o in orders:
        agg[o.price] += o.qty
    return sorted(agg.items(), reverse=reverse)


def test_depth_and_trades_match_the_python_baseline_on_random_flow():
    for seed in range(60):
        rnd = random.Random(seed)
        eng, base = MatchingEngine(), OrderBook()
        for i in range(rnd.choice([0, 1, 20, 150])):
            side = rnd.randint(0, 1)
            price = round(100 + rnd.randint(-6, 6) * 0.01, 2)
            qty = rnd.choice([1, 5, 50, 500])
            eng.add_order(i, side, price, qty, i + 1)
            base.add_order(Order(i, "buy" if side == 0 else "sell", price, qty, i + 1))
        trades = lambda ts: [(t["buy_id"], t["sell_id"], t["price"], t["qty"]) for t in ts]
        assert trades(eng.get_trades()) == trades(base.trades)
        for ml in (1, 3, 50):
            d = eng.get_book_depth(ml)
            assert d["bid_levels"] == _levels(base.buy_orders, True)[:ml]
            assert d["ask_levels"] == _levels(base.sell_orders, False)[:ml]
            assert d["total_bid_qty"] == sum(o.qty for o in base.buy_orders)
            assert d["total_ask_qty"] == sum(o.qty for o in base.sell_orders)