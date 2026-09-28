"""Unit and integration test suite for ChronosMatch Latency Dashboard (Member 3)."""

import curses
import pytest
from unittest.mock import MagicMock

from src.monitor.latency_monitor import (
    BUY,
    SELL,
    LatencyMonitor,
    CursesDashboard,
    OrderBookTop,
    LatencyStats,
    MarketState,
    calculate_percentile,
    generate_sparkline,
    get_matching_engine,
    main,
)


class DummyWindow:
    """Mock curses window to test drawing and boundary safety without a real terminal."""

    def __init__(self, height: int = 30, width: int = 90):
        self.height = height
        self.width = width
        self.strings: list[tuple[int, int, str, int]] = []
        self.erased = False
        self.refreshed = False

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.erased = True
        self.strings.clear()

    def refresh(self) -> None:
        self.refreshed = True

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            raise curses.error("Coordinates out of range")
        self.strings.append((y, x, text, attr))


# ---------------------------------------------------------------------------
# Core Monitor Tests
# ---------------------------------------------------------------------------

def test_monitor_initialization():
    monitor = LatencyMonitor(seed=42, base_price=100.0)
    assert monitor.engine is not None
    assert monitor.simulator is not None
    assert len(monitor.all_latencies_ns) == 0
    assert len(monitor.rolling_latencies_ns) == 0

    book = monitor.get_top_of_book()
    assert book.best_bid is None
    assert book.best_ask is None
    assert book.spread is None


def test_monitor_step_generates_orders():
    monitor = LatencyMonitor(seed=123, base_price=100.0)
    latencies = monitor.step(num_orders=5)

    assert len(latencies) == 5
    assert len(monitor.all_latencies_ns) == 5
    assert all(lat > 0 for lat in latencies)
    assert monitor.engine.get_order_count() == 5


def test_manual_order_entry_and_matching():
    monitor = LatencyMonitor(seed=42)

    # Add resting BUY order
    monitor.add_custom_order(order_id=1, side="BUY", price=100.0, quantity=10)
    book = monitor.get_top_of_book()
    assert book.best_bid == 100.0
    assert book.best_ask is None
    assert book.spread is None
    assert book.bid_order_count == 1

    # Add matching SELL order
    monitor.add_custom_order(order_id=2, side="SELL", price=100.0, quantity=10)
    trades = monitor.engine.get_trades()
    assert len(trades) == 1
    assert trades[0]["buy_id"] == 1
    assert trades[0]["sell_id"] == 2
    assert trades[0]["price"] == 100.0
    assert trades[0]["qty"] == 10

    # Book should now be empty
    book_after = monitor.get_top_of_book()
    assert book_after.best_bid is None
    assert book_after.best_ask is None


def test_top_of_book_and_spread_calculation():
    monitor = LatencyMonitor(seed=42)

    # Rest multiple bids and asks
    monitor.add_custom_order(order_id=1, side=BUY, price=99.0, quantity=50)
    monitor.add_custom_order(order_id=2, side=BUY, price=99.5, quantity=30)
    monitor.add_custom_order(order_id=3, side=SELL, price=100.5, quantity=25)
    monitor.add_custom_order(order_id=4, side=SELL, price=101.0, quantity=40)

    book = monitor.get_top_of_book(depth=5)
    assert book.best_bid == 99.5
    assert book.best_ask == 100.5
    assert book.spread == pytest.approx(1.0)
    assert book.mid_price == pytest.approx(100.0)
    assert book.spread_bps == pytest.approx(100.0)  # (1.0 / 100.0) * 10,000 = 100 bps
    assert not book.is_crossed

    # Verify depth levels
    assert len(book.bid_levels) == 2
    assert book.bid_levels[0] == (99.5, 30)
    assert book.bid_levels[1] == (99.0, 50)
    assert len(book.ask_levels) == 2
    assert book.ask_levels[0] == (100.5, 25)
    assert book.ask_levels[1] == (101.0, 40)


def test_order_book_crossed_status():
    book = OrderBookTop(best_bid=102.0, best_ask=100.0, is_crossed=True)
    assert book.is_crossed is True


def test_latency_statistics():
    monitor = LatencyMonitor()
    # Mock specific latencies (in nanoseconds)
    monitor.all_latencies_ns = [1000, 2000, 3000, 4000, 5000]
    monitor._last_latency_ns = 5000
    monitor.rolling_latencies_ns.extend(monitor.all_latencies_ns)

    stats = monitor.get_latency_stats()
    assert stats.count == 5
    assert stats.min_ns == 1000
    assert stats.max_ns == 5000
    assert stats.mean_ns == 3000.0
    assert stats.p50_ns == 3000.0
    assert stats.min_us == 1.0
    assert stats.max_us == 5.0
    assert stats.mean_us == 3.0
    assert stats.p50_us == 3.0
    assert len(stats.sparkline) > 0


def test_calculate_percentile_helper():
    assert calculate_percentile([], 50) == 0.0
    assert calculate_percentile([42.0], 50) == 42.0
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert calculate_percentile(vals, 0) == 10.0
    assert calculate_percentile(vals, 50) == 30.0
    assert calculate_percentile(vals, 100) == 50.0


def test_generate_sparkline_helper():
    assert generate_sparkline([], width=5) == "....."
    flat = generate_sparkline([10.0, 10.0, 10.0], width=3)
    assert len(flat) == 3
    varying = generate_sparkline([1.0, 5.0, 10.0, 20.0], width=4)
    assert len(varying) == 4


def test_market_state_aggregation():
    monitor = LatencyMonitor(seed=42)
    monitor.add_custom_order(order_id=1, side="BUY", price=100.0, quantity=10)
    monitor.add_custom_order(order_id=2, side="SELL", price=100.0, quantity=10)

    state = monitor.get_market_state(is_paused=False, rate_target=50)
    assert state.orders_count == 2
    assert state.trades_count == 1
    assert state.total_volume == 10
    assert state.total_notional == 1000.0
    assert state.vwap == 100.0
    assert len(state.recent_trades) == 1
    assert state.is_paused is False
    assert state.rate_target == 50


def test_render_text_snapshot():
    monitor = LatencyMonitor(seed=42)
    monitor.step(num_orders=10)
    snapshot = monitor.render_text_snapshot(depth=3, trade_count=3)

    assert "CHRONOSMATCH LATENCY DASHBOARD" in snapshot
    assert "TOP OF ORDER BOOK & SPREAD" in snapshot
    assert "LATENCY METRICS" in snapshot
    assert "RECENT MATCHED TRADES" in snapshot


def test_monitor_reset():
    monitor = LatencyMonitor(seed=42)
    monitor.step(num_orders=15)
    assert monitor.engine.get_order_count() == 15
    assert len(monitor.all_latencies_ns) == 15

    monitor.reset(new_seed=99)
    assert monitor.engine.get_order_count() == 0
    assert len(monitor.all_latencies_ns) == 0
    assert monitor.seed == 99


# ---------------------------------------------------------------------------
# Curses UI & Bounds Safety Tests
# ---------------------------------------------------------------------------

def test_curses_safe_addstr():
    win = DummyWindow(height=10, width=20)
    dashboard = CursesDashboard(monitor=LatencyMonitor())

    # Inside window
    dashboard.safe_addstr(win, 2, 2, "hello")
    assert any("hello" in s[2] for s in win.strings)

    # Outside window boundaries should NOT raise exception
    dashboard.safe_addstr(win, -1, 0, "out-of-bounds")
    dashboard.safe_addstr(win, 15, 0, "out-of-bounds")
    dashboard.safe_addstr(win, 0, 30, "out-of-bounds")


def test_curses_draw_box():
    win = DummyWindow(height=20, width=40)
    dashboard = CursesDashboard(monitor=LatencyMonitor())

    dashboard.draw_box(win, 2, 2, 5, 20, title="TEST BOX")
    assert any("TEST BOX" in s[2] for s in win.strings)


def test_curses_render_small_window_guard():
    small_win = DummyWindow(height=6, width=35)  # smaller than min 45x8
    dashboard = CursesDashboard(monitor=LatencyMonitor())

    # Should not crash, renders warning
    dashboard.render(small_win)
    assert any("too small" in s[2].lower() for s in small_win.strings)


def test_curses_render_compact_window():
    compact_win = DummyWindow(height=12, width=62)  # typical smaller terminal (e.g. 62x12)
    monitor = LatencyMonitor(seed=42)
    monitor.step(num_orders=5)
    dashboard = CursesDashboard(monitor=monitor)

    dashboard.render(compact_win)
    rendered_text = " ".join(s[2] for s in compact_win.strings)
    assert "CHRONOSMATCH" in rendered_text
    assert "BID:" in rendered_text
    assert "ASK:" in rendered_text
    assert "SPREAD:" in rendered_text
    assert "LATENCY:" in rendered_text


def test_curses_render_standard_window():
    win = DummyWindow(height=30, width=100)
    monitor = LatencyMonitor(seed=42)
    monitor.step(num_orders=10)
    dashboard = CursesDashboard(monitor=monitor)

    dashboard.render(win)
    rendered_text = " ".join(s[2] for s in win.strings)
    assert "CHRONOSMATCH" in rendered_text
    assert "BEST BID" in rendered_text
    assert "BEST ASK" in rendered_text
    assert "SPREAD" in rendered_text


# ---------------------------------------------------------------------------
# CLI Entrypoint Tests
# ---------------------------------------------------------------------------

def test_main_cli_once(capsys):
    ret = main(["--once", "--max-orders", "5", "--seed", "42"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "CHRONOSMATCH LATENCY DASHBOARD" in captured.out
    assert "TOP OF ORDER BOOK" in captured.out
