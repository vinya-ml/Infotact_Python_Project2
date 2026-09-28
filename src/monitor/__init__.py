"""ChronosMatch Monitor Module (Member 3).

Provides the raw curses terminal UI latency dashboard, Top of Order Book
monitor, Bid/Ask spread tracker, and real-time market metrics.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .latency_monitor import (
        LatencyMonitor,
        CursesDashboard,
        OrderBookTop,
        LatencyStats,
        MarketState,
        get_matching_engine,
        main,
    )

def __getattr__(name: str):
    if name in (
        "LatencyMonitor",
        "CursesDashboard",
        "OrderBookTop",
        "LatencyStats",
        "MarketState",
        "get_matching_engine",
        "main",
    ):
        from . import latency_monitor
        return getattr(latency_monitor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "LatencyMonitor",
    "CursesDashboard",
    "OrderBookTop",
    "LatencyStats",
    "MarketState",
    "get_matching_engine",
    "main",
]
