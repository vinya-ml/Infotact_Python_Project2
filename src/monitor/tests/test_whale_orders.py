"""Automated test suite for Whale-Order Detection & Visualization (Member 3)."""

import pytest
from src.monitor.latency_monitor import LatencyMonitor, WhaleEvent


def test_whale_order_absolute_threshold_detection():
    """Verify orders and trades exceeding the absolute threshold are captured and tagged."""
    monitor = LatencyMonitor(
        whale_threshold=500,
        whale_mode="absolute",
        enable_persistence=False,
    )

    # Submit normal small order (should NOT trigger whale)
    monitor.add_custom_order(order_id=1, side="BUY", price=90.0, quantity=50)
    assert len(monitor.whale_events) == 0

    # Submit whale BUY order (qty=600 >= 500)
    monitor.add_custom_order(order_id=2, side="BUY", price=100.0, quantity=600)
    assert len(monitor.whale_events) == 1
    w1 = monitor.whale_events[0]
    assert w1.event_type == "RESTING_ORDER"
    assert w1.side == "BUY"
    assert w1.quantity == 600
    assert w1.notional == 100.0 * 600

    # Submit matching whale SELL order (qty=600)
    # This should trigger BOTH a resting whale order event and an executed whale trade event!
    monitor.add_custom_order(order_id=3, side="SELL", price=100.0, quantity=600)

    # Now we should have resting buy, resting sell, and matched trade
    events = list(monitor.whale_events)
    assert len(events) == 3

    trade_events = [e for e in events if e.event_type == "MATCHED_TRADE"]
    assert len(trade_events) == 1
    assert trade_events[0].quantity == 600
    assert trade_events[0].price == 100.0


def test_whale_order_relative_threshold_detection():
    """Verify dynamic relative threshold adjusts based on observed order quantities."""
    monitor = LatencyMonitor(
        whale_threshold=500,
        whale_mode="relative",
        enable_persistence=False,
    )

    # Feed 25 small orders of size 20 to 50
    for i in range(25):
        monitor.add_custom_order(order_id=100 + i, side="BUY", price=50.0 + (i * 0.1), quantity=30)

    # In relative mode, 95th percentile will be ~30, but floored at 100
    threshold = monitor.get_current_whale_threshold()
    assert threshold == 100

    # Submit an order of size 120 (above 100 relative threshold, even though below 500 absolute)
    monitor.add_custom_order(order_id=999, side="BUY", price=55.0, quantity=120)
    whales = [e for e in monitor.whale_events if e.order_id == 999]
    assert len(whales) == 1
    assert whales[0].quantity == 120


def test_whale_section_rendering_snapshot():
    """Verify whale orders and trade sweeps are cleanly rendered in text snapshot."""
    monitor = LatencyMonitor(whale_threshold=200, enable_persistence=False)

    monitor.add_custom_order(order_id=1, side="BUY", price=150.0, quantity=300)
    monitor.add_custom_order(order_id=2, side="SELL", price=150.0, quantity=300)

    snapshot = monitor.render_text_snapshot()
    assert "WHALE ORDERS & LARGE SWEEPS" in snapshot
    assert "[WHALE ORDER]" in snapshot
    assert "[WHALE TRADE]" in snapshot
    assert "$150.00" in snapshot
