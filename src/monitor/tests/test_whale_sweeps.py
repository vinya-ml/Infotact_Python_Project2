"""Tests for multi-level whale sweep detection (Week 4 UI polish).

PDF requirement: "visually highlight when a massive Whale order clears
multiple price levels."

A WHALE_SWEEP is one incoming order that (a) filled across at least
`sweep_min_levels` distinct price levels and (b) executed at least the
whale threshold in total quantity.
"""

import collections
import os
import sys

import pytest

# Make the project root importable so this file also runs on its own
# (e.g. `pytest src/monitor/tests/test_whale_sweeps.py`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.analysis.simulator import MockMarketSimulator
from src.model.baseline_matcher import Order, OrderBook
from src.monitor.latency_monitor import LatencyMonitor, WhaleEvent, format_whale_event


def make_monitor(threshold=500, **kw):
    return LatencyMonitor(whale_threshold=threshold, whale_mode="absolute",
                          enable_persistence=False, **kw)


def sweeps(monitor):
    return [e for e in monitor.whale_events if e.event_type == "WHALE_SWEEP"]


def load_asks(monitor, levels, qty_each, start_id=1, base=100.0):
    """Rest one SELL order per price level, 0.10 apart, starting at `base`."""
    for i in range(levels):
        monitor.add_custom_order(order_id=start_id + i, side="SELL",
                                 price=round(base + 0.1 * i, 2), quantity=qty_each)


def test_whale_order_clearing_four_levels_is_flagged():
    m = make_monitor(500)
    load_asks(m, 4, 200)
    m.add_custom_order(order_id=50, side="BUY", price=100.3, quantity=800)
    (s,) = sweeps(m)
    assert s.side == "BUY"
    assert s.order_id == 50
    assert s.levels_cleared == 4
    assert s.quantity == 800
    assert s.price == 100.0            # first (best) level hit
    assert s.price_end == 100.3        # last (worst) level hit
    assert s.notional == pytest.approx(200 * (100.0 + 100.1 + 100.2 + 100.3))


def test_single_level_whale_trade_is_not_a_sweep():
    m = make_monitor(500)
    load_asks(m, 1, 600)
    m.add_custom_order(order_id=50, side="BUY", price=100.0, quantity=600)
    assert sweeps(m) == []


def test_multi_level_fill_below_whale_size_is_not_flagged():
    m = make_monitor(500)
    load_asks(m, 4, 100)
    m.add_custom_order(order_id=50, side="BUY", price=100.3, quantity=400)
    assert sweeps(m) == []


def test_sell_side_sweep_is_flagged():
    m = make_monitor(500)
    for i in range(4):
        m.add_custom_order(order_id=1 + i, side="BUY",
                           price=round(100.0 - 0.1 * i, 2), quantity=200)
    m.add_custom_order(order_id=50, side="SELL", price=99.7, quantity=800)
    (s,) = sweeps(m)
    assert s.side == "SELL"
    assert s.levels_cleared == 4
    assert s.price == 100.0 and s.price_end == 99.7


def test_partially_filled_whale_that_rests_the_remainder_is_still_a_sweep():
    m = make_monitor(500)
    load_asks(m, 3, 200)                       # only 600 available
    m.add_custom_order(order_id=50, side="BUY", price=100.2, quantity=900)
    (s,) = sweeps(m)
    assert s.levels_cleared == 3
    assert s.quantity == 600                   # executed, not the 900 submitted


def test_min_levels_setting_is_respected():
    m = make_monitor(500, sweep_min_levels=3)
    load_asks(m, 2, 400)
    m.add_custom_order(order_id=50, side="BUY", price=100.1, quantity=800)
    assert sweeps(m) == []                     # only 2 levels, 3 required

    m2 = make_monitor(500, sweep_min_levels=2)
    load_asks(m2, 2, 400)
    m2.add_custom_order(order_id=50, side="BUY", price=100.1, quantity=800)
    assert len(sweeps(m2)) == 1


def test_sweep_detection_can_be_switched_off():
    m = make_monitor(500, sweep_min_levels=0)
    load_asks(m, 4, 200)
    m.add_custom_order(order_id=50, side="BUY", price=100.3, quantity=800)
    assert sweeps(m) == []


def test_several_fills_at_one_price_level_are_not_a_multi_level_sweep():
    """Two resting orders at the SAME price: two trades, but only one level."""
    m = make_monitor(500)
    m.add_custom_order(order_id=1, side="SELL", price=100.0, quantity=400)
    m.add_custom_order(order_id=2, side="SELL", price=100.0, quantity=400)
    m.add_custom_order(order_id=50, side="BUY", price=100.0, quantity=800)
    assert len(m.engine.get_trades()) == 2     # really two fills ...
    assert sweeps(m) == []                     # ... but a single level


def test_trades_of_different_orders_are_never_merged_into_a_sweep():
    """Two separate single-level whale fills, back to back, at different prices."""
    m = make_monitor(500)
    m.add_custom_order(order_id=1, side="SELL", price=100.0, quantity=600)
    m.add_custom_order(order_id=2, side="SELL", price=100.1, quantity=600)
    m.add_custom_order(order_id=10, side="BUY", price=100.0, quantity=600)   # level 100.0
    m.add_custom_order(order_id=11, side="BUY", price=100.1, quantity=600)   # level 100.1
    assert sweeps(m) == []


def test_sweep_is_shown_in_the_text_snapshot():
    m = make_monitor(500)
    load_asks(m, 4, 200)
    m.add_custom_order(order_id=50, side="BUY", price=100.3, quantity=800)
    text = m.render_text_snapshot()
    assert "[WHALE SWEEP]" in text
    assert "4 levels" in text
    assert "$100.00->$100.30" in text


def test_format_whale_event_keeps_the_old_tags_for_other_events():
    order = WhaleEvent("RESTING_ORDER", "BUY", 100.0, 600, 60000.0)
    trade = WhaleEvent("MATCHED_TRADE", "TRADE", 100.0, 600, 60000.0)
    assert format_whale_event(order)[0] == "[WHALE ORDER]"
    assert format_whale_event(trade)[0] == "[WHALE TRADE]"


class _RecordingDeque(collections.deque):
    """Keeps every event ever appended (the monitor's own deque drops old ones)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.all = []

    def append(self, item):
        self.all.append(item)
        super().append(item)


def test_monitor_agrees_with_an_independent_replay_of_the_same_order_stream():
    """Replay the monitor's exact random orders through the baseline matcher and
    work out the sweeps separately; the monitor must report the same ones."""
    seed, base_price, threshold, n_orders = 7, 100.0, 150, 3000

    monitor = LatencyMonitor(seed=seed, base_price=base_price, whale_threshold=threshold,
                             whale_mode="absolute", enable_persistence=False,
                             sweep_min_levels=2)
    monitor.whale_events = _RecordingDeque(maxlen=50)
    for _ in range(n_orders):
        monitor.step(1)
    got = [(e.order_id, e.levels_cleared, e.quantity, e.price, e.price_end)
           for e in monitor.whale_events.all if e.event_type == "WHALE_SWEEP"]

    sim = MockMarketSimulator(seed=seed, base_price=base_price)
    book = OrderBook()
    expected = []
    for ts in range(1, n_orders + 1):
        o = sim._next_order(whale_prob=0.04)
        before = len(book.trades)
        book.add_order(Order(int(o["order_id"]), "buy" if o["side"] == "BUY" else "sell",
                             float(o["price"]), int(o["quantity"]), ts))
        new = book.trades[before:]
        prices = []
        for t in new:
            if t["price"] not in prices:
                prices.append(t["price"])
        executed = sum(t["qty"] for t in new)
        if len(prices) >= 2 and executed >= threshold:
            expected.append((int(o["order_id"]), len(prices), executed, prices[0], prices[-1]))

    assert len(expected) >= 3, "scenario must contain real sweeps to be a meaningful test"
    assert got == expected


def _push_whale_noise(m, count):
    """Newer whale-sized events that are NOT sweeps (large resting buys far from the market)."""
    for i in range(count):
        m.add_custom_order(order_id=900 + i, side="BUY", price=80.0 + i * 0.01, quantity=700)


def test_latest_sweep_stays_pinned_after_newer_whale_events():
    m = make_monitor(500)
    load_asks(m, 4, 200)
    m.add_custom_order(order_id=50, side="BUY", price=100.3, quantity=800)
    _push_whale_noise(m, 10)                       # far more than the panel can show
    assert "[WHALE SWEEP]" not in "\n".join(
        f"{format_whale_event(w)[0]}" for w in m.get_market_state().recent_whale_events)
    text = m.render_text_snapshot()                # ...yet the sweep is still on screen
    assert text.count("[WHALE SWEEP]") == 1
    assert "4 levels" in text


def test_sweep_is_not_listed_twice_when_it_is_already_in_the_recent_events():
    m = make_monitor(500)
    load_asks(m, 4, 200)
    m.add_custom_order(order_id=50, side="BUY", price=100.3, quantity=800)
    assert m.render_text_snapshot().count("[WHALE SWEEP]") == 1


def test_select_whale_events_handles_tiny_windows_and_no_sweep():
    from src.monitor.latency_monitor import MarketState, select_whale_events
    st = MarketState()
    assert select_whale_events(st, 4) == []
    ev = [WhaleEvent("RESTING_ORDER", "BUY", 1.0, 600, 600.0, order_id=i) for i in range(5)]
    st.recent_whale_events = ev
    assert select_whale_events(st, 0) == []
    assert select_whale_events(st, 2) == ev[-2:]
    st.last_whale_sweep = WhaleEvent("WHALE_SWEEP", "BUY", 1.0, 900, 900.0, levels_cleared=3)
    assert select_whale_events(st, 1) == [st.last_whale_sweep]
    assert select_whale_events(st, 3)[0] is st.last_whale_sweep
    assert len(select_whale_events(st, 3)) == 3


class _FakeScreen:
    """Minimal curses window: records (y, x, text, attr) for every string drawn."""

    def __init__(self, height=40, width=110):
        self.height, self.width = height, width
        self.strings = []

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.strings.clear()

    def refresh(self):
        pass

    def addstr(self, y, x, text, attr=0):
        import curses
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            raise curses.error("out of range")
        self.strings.append((y, x, text, attr))


def test_curses_view_highlights_the_sweep_with_inverted_colours():
    import curses
    from src.monitor.latency_monitor import CursesDashboard

    m = make_monitor(500)
    load_asks(m, 4, 200)
    m.add_custom_order(order_id=50, side="BUY", price=100.3, quantity=800)
    m.add_custom_order(order_id=60, side="BUY", price=90.0, quantity=700)   # an ordinary whale order

    screen = _FakeScreen()
    CursesDashboard(monitor=m).render(screen)

    sweep_rows = [s for s in screen.strings if "[WHALE SWEEP]" in s[2]]
    order_rows = [s for s in screen.strings if "[WHALE ORDER]" in s[2]]
    assert len(sweep_rows) == 1
    assert "4 levels" in sweep_rows[0][2]
    assert sweep_rows[0][3] & curses.A_REVERSE          # the sweep is inverted ...
    assert order_rows and not (order_rows[0][3] & curses.A_REVERSE)   # ... ordinary whales are not